"""Cookie-based community scanning, standalone so the WORKER and the web app
both use the same code.

Reading a private community over HTTP with the member session cookie -- posts,
comments, replies -- classifying and filing leads. No browser. The heavy work
lives here (not inside a web request) so the Railway worker can run it fast on
a warm, pooled connection, while the dashboard just enqueues a job.
"""

from __future__ import annotations

import datetime as _dt
import logging
import time
from typing import Any

from sqlalchemy import select, update

from circle_leads.config.settings import Requirements
from circle_leads.storage.activity import log_activity
from circle_leads.storage.database import Database
from circle_leads.storage.models import (
    CircleConnection, Community, ConnectionPriority, ConnectionState, ReplaySession,
    SCAN_ORDER,
)

logger = logging.getLogger(__name__)


def cookie_hosts_vip_first(db: Database) -> list[str]:
    """Hosts with a stored session cookie, VIP first, paused excluded."""
    with db.session() as s:
        with_cookie = {r.host for r in s.scalars(select(ReplaySession)).all()}
        rows = [c for c in s.scalars(select(CircleConnection)).all()
                if c.host in with_cookie
                and c.priority != ConnectionPriority.PAUSED.value]
        rows.sort(key=lambda c: (SCAN_ORDER.get(c.priority, 1), c.host))
        return [c.host for c in rows]


# How far a re-read looks back past the previous complete read: a comment on a
# week-old post is still worth catching, and an edit or a slow clock can put a
# post a little behind the stamp.
RESCAN_OVERLAP = _dt.timedelta(days=7)


def _rescan_since(db: Database, host: str) -> _dt.datetime | None:
    """Where a re-read of ``host`` may start: its last complete, successful
    scan minus RESCAN_OVERLAP. None -- read everything -- before the first
    scan, and after a failed, partial or empty one."""
    with db.session() as s:
        conn = s.scalar(select(CircleConnection).where(CircleConnection.host == host))
        if (conn is None or conn.last_sync_at is None
                or conn.state != ConnectionState.CONNECTED.value
                or "partial" in (conn.state_detail or "")
                or not conn.spaces_readable):
            return None
        return conn.last_sync_at - RESCAN_OVERLAP


def _age_since(requirements: Requirements) -> _dt.datetime | None:
    """The oldest post worth reading: ``max_post_age_days`` back from now, or
    None when the cap is off (0). Older posts are dropped at triage anyway
    (triage/pipeline.py), so reading them only costs requests."""
    days = int(getattr(requirements, "max_post_age_days", 0) or 0)
    if days <= 0:
        return None
    return _dt.datetime.utcnow() - _dt.timedelta(days=days)


def scan_cookie_host(db: Database, requirements: Requirements, host: str, *,
                     max_pages: int = 5, time_budget: float | None = None,
                     fast_pause: bool = False, use_llm: bool = False,
                     with_comments: bool = True) -> dict[str, Any]:
    """Read one community with its stored cookie and ingest posts+comments.

    Returns a summary and updates the connection state. ``time_budget`` (s)
    stops early and flags the run ``partial``.
    """
    from circle_leads.web.replay_store import SessionUnreadable, load_cookies_strict
    from circle_leads.scraper.member_api_reader import (
        MemberApiReader, SessionInvalid, ChallengeHit, ProfileIncomplete, fetch_space_posts,
    )
    from circle_leads.triage.pipeline import triage_records
    from circle_leads.discovery.discover_communities import unique_slug_for_host

    started = time.time()
    # Stable slug for this host. NOT host.split(".")[0] -- that collapsed every
    # www.* host to "www" and every community.* host to "community", so
    # unrelated communities piled onto one row (see WORKLOG P10).
    # And NOT community_slug_for_host either: it answers "which organisation is
    # this?", so forum.acme.com and shop.acme.com both reduce to "acme". Since
    # get_or_create_community matches on `slug OR url`, a shared slug merges two
    # unrelated communities onto one row and discards the second URL -- silently.
    slug = unique_slug_for_host(host)
    unreadable: SessionUnreadable | None = None
    try:
        cookies = {c["name"]: c["value"] for c in (load_cookies_strict(db, host) or [])}
    except SessionUnreadable as exc:
        cookies, unreadable = {}, exc
    # Never read past the age cap. The 2026-09-10 decision (P15) was to cut
    # old posts at reading on every path; the public harvest got it, this one
    # did not, so every pass read up to 100 posts per space plus comments and
    # threw the old ones away at triage. On 2026-11-06 that made one pass over
    # the cookie communities take eight hours, asaporg alone almost four.
    rescan = _rescan_since(db, host)
    age_since = _age_since(requirements)
    since = max((d for d in (rescan, age_since) if d is not None), default=None)
    state = ConnectionState.CONNECTED
    detail = ""
    total = readable = spaces_total = leads = 0
    partial = False
    try:
        if unreadable is not None:
            raise unreadable
        # 1.2s between requests keeps a scan at <= ~50/min: Circle limits
        # per IP (~100/min got every host 429'd), and on the Mac the worker
        # shares that IP with enrichment and join runs.
        reader = MemberApiReader(host, cookies=cookies,
                                 request_pause=0.2 if fast_pause else 1.2)
        try:
            spaces = reader.list_spaces()   # doubles as the session check
        except SessionInvalid:
            state = ConnectionState.SESSION_EXPIRED
            detail = "Session cookie invalid or expired -- refresh it."
            spaces = None
        if spaces is not None:
            spaces_total = len(spaces)
            for sp in spaces:
                if time_budget and (time.time() - started) > time_budget:
                    partial = True
                    break
                try:
                    cap = 8 if time_budget else 25
                    recs = fetch_space_posts(
                        reader, sp["id"], max_pages=max_pages,
                        with_comments=with_comments, max_comment_posts=cap,
                        space_slug=sp.get("slug"), since=since)
                except SessionInvalid:
                    continue
                if not recs:
                    continue
                readable += 1
                total += len(recs)
                # One shared DB connection for this space's writes (big win over
                # a per-post pooler checkout on Supabase).
                with db.shared_session():
                    res = triage_records(
                        db, recs, requirements, community=slug,
                        source_url=f"https://{host}", use_llm=use_llm,
                    )
                leads += len(res.leads)
            suffix = " (partial — scan again to continue)" if partial else ""
            if total:
                detail = (f"{total} post(s), {leads} lead(s) from "
                          f"{readable}/{spaces_total} space(s){suffix}")
            elif rescan is not None:
                detail = f"No new posts since the last read ({spaces_total} space(s) visible){suffix}."
            elif since is not None:
                detail = (f"No posts newer than {requirements.max_post_age_days} day(s) "
                          f"({spaces_total} space(s) visible){suffix}.")
            else:
                detail = f"No readable posts ({spaces_total} space(s) visible){suffix}."
    except SessionInvalid:
        state = ConnectionState.SESSION_EXPIRED
        detail = "Session rejected -- refresh the cookie."
    except SessionUnreadable as exc:
        # Not an expired cookie: a fresh paste would fail the same way.
        state = ConnectionState.ERROR
        detail = str(exc)
    except ChallengeHit as exc:
        state = ConnectionState.ERROR
        detail = str(exc)
    except ProfileIncomplete as exc:
        # Joined but the profile step is open: reported as such instead of the
        # old silent "connected, 0 spaces" (the 400 used to read as no spaces).
        state = ConnectionState.ERROR
        detail = str(exc)
    except Exception as exc:  # noqa: BLE001 - one bad host must not stop a batch
        state = ConnectionState.ERROR
        detail = f"{exc.__class__.__name__}: {exc}"

    with db.session() as s:
        conn = s.scalar(select(CircleConnection).where(CircleConnection.host == host))
        if conn is None:
            conn = CircleConnection(host=host)
            s.add(conn)
        conn.state = state.value
        conn.state_detail = detail or None
        if since is not None and state == ConnectionState.CONNECTED:
            # A re-read -- and a read held to the age cap -- counts only the
            # spaces with something new in them. Stored
            # as is, a quiet day wrote 0, and _rescan_since took that for an
            # empty read, so every other pass re-read the whole archive:
            # entrepreneur-bootcamp 205 posts at 21:45, 0 at 03:41 (2026-09-29/30).
            # The spaces the session reads are the ones the full read found.
            conn.spaces_readable = max(readable, conn.spaces_readable or 0)
        else:
            conn.spaces_readable = readable
        conn.spaces_total = spaces_total
        conn.last_sync_at = _dt.datetime.utcnow()
        if state == ConnectionState.CONNECTED and spaces_total and not partial:
            # A complete member read is a read. Only the public harvest used to
            # stamp one, so a community read every six hours with its session
            # (community.freelancemvp.com) sat on the dashboard as never read.
            s.execute(update(Community).where(Community.host == host)
                      .values(last_synced_at=conn.last_sync_at))
        log_activity(
            s, kind="ingest",
            level="warning" if state != ConnectionState.CONNECTED
            else ("success" if leads else "info"),
            community=slug,
            summary=(f"HTTP scan of {host}: {total} post(s), {leads} lead(s) "
                     f"from {readable} space(s)"
                     + (f" — {detail}" if state != ConnectionState.CONNECTED else "")),
            items_seen=total, leads_found=leads,
        )
    return {"host": host, "state": state.value, "posts": total,
            "spaces_readable": readable, "leads": leads, "detail": detail,
            "partial": partial}


# --- checking a session the moment it is saved ---------------------------------

def verify_session(host: str, cookies: dict[str, str], *,
                   timeout: float = 15.0) -> dict[str, Any]:
    """Ask Circle once whether these cookies read ``host``.

    One GET of /internal_api/spaces, the call every scan opens with. A pasted
    session used to be stored on faith: the community kept its old "session
    expired" until the worker's next read -- hours away behind a long harvest --
    so the person pasting saw nothing change and could not tell a good cookie
    from a bad one (2026-11-06).

    ``verdict``: "valid" (Circle lists spaces), "no_spaces" (accepted, but
    nothing visible: probably not a member), "rejected" (401: not logged in),
    "forbidden" (403: not a member, or this address refused -- the check runs
    from Vercel, the reads from the droplet), "profile_incomplete", or
    "unverified" (no clear answer from here). Cookie values never appear in
    the result.
    """
    from circle_leads.scraper.member_api_reader import (
        ChallengeHit, MemberApiReader, ProfileIncomplete, SessionInvalid,
    )
    reader = MemberApiReader(host, cookies=cookies, request_pause=0, timeout=timeout)
    try:
        spaces = reader.probe_spaces()
    except SessionInvalid as exc:
        if exc.status == 403:
            return {"verdict": "forbidden", "spaces": 0,
                    "detail": ("Circle answered 403 to the check from the dashboard: this "
                               "account may not be a member, or the dashboard's address is "
                               "refused. The worker will try it from the server.")}
        return {"verdict": "rejected", "spaces": 0, "detail": str(exc)}
    except ProfileIncomplete as exc:
        return {"verdict": "profile_incomplete", "spaces": 0, "detail": str(exc)}
    except ChallengeHit:
        return {"verdict": "unverified", "spaces": 0,
                "detail": "Cloudflare challenged the check from the dashboard."}
    except Exception as exc:  # noqa: BLE001 - network trouble is not a verdict
        return {"verdict": "unverified", "spaces": 0,
                "detail": f"Could not reach {host}: {exc.__class__.__name__}."}
    if spaces is None:
        return {"verdict": "unverified", "spaces": 0,
                "detail": "Circle answered without JSON; the worker will check it."}
    if not spaces:
        return {"verdict": "no_spaces", "spaces": 0,
                "detail": (f"Circle accepted the session but shows it no spaces: this "
                           f"account is probably not a member of {host}.")}
    return {"verdict": "valid", "spaces": len(spaces),
            "detail": f"Circle lists {len(spaces)} space(s) for this session."}


_CHECK_STATE = {
    "valid": ConnectionState.CONNECTED,
    "no_spaces": ConnectionState.ACCESS_DENIED,
    "forbidden": ConnectionState.ACCESS_DENIED,
    "profile_incomplete": ConnectionState.ERROR,
    # The old verdict was about the old cookie; this one is not known yet.
    "unverified": ConnectionState.NOT_CONNECTED,
}


def apply_session_check(db: Database, host: str, check: dict[str, Any]) -> None:
    """Put a fresh session's check on the connection, replacing the old cookie's
    verdict. The read stamps (last_sync_at, spaces_readable) are left alone: no
    posts were read, and a failed scan already zeroed spaces_readable, so the
    next read starts from the full archive (see _rescan_since).

    A working session also clears the feed watcher's last answer when that was
    a 401: it was the old cookie's, and "Needs attention" kept listing the
    host as "needs verification" until the watcher came round again -- ten
    minutes and more behind a spent request budget (2026-11-06)."""
    from circle_leads.storage.models import WatchState

    state = _CHECK_STATE.get(check.get("verdict"))
    if state is None:
        return
    when = _dt.datetime.utcnow().strftime("%H:%M UTC")
    with db.session() as s:
        if check.get("verdict") == "valid":
            s.execute(update(WatchState).where(
                WatchState.host == host, WatchState.last_status == "unauthorized",
            ).values(last_status=None,
                     last_detail=f"New session checked {when}; next feed check pending."))
        conn = s.scalar(select(CircleConnection).where(CircleConnection.host == host))
        if conn is None:
            return
        # _rescan_since reads "partial" in the detail as "finish the archive".
        unfinished = "partial" in (conn.state_detail or "")
        conn.state = state.value
        conn.state_detail = (f"New session saved {when}: {check.get('detail') or ''}".strip()
                             + (" Last read was partial." if unfinished else ""))
