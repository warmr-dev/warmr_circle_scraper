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

from sqlalchemy import select

from circle_leads.config.settings import Requirements
from circle_leads.storage.activity import log_activity
from circle_leads.storage.database import Database
from circle_leads.storage.models import (
    CircleConnection, ConnectionPriority, ConnectionState, ReplaySession,
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


def scan_cookie_host(db: Database, requirements: Requirements, host: str, *,
                     max_pages: int = 5, time_budget: float | None = None,
                     fast_pause: bool = False, use_llm: bool = False,
                     with_comments: bool = True) -> dict[str, Any]:
    """Read one community with its stored cookie and ingest posts+comments.

    Returns a summary and updates the connection state. ``time_budget`` (s)
    stops early and flags the run ``partial``.
    """
    from circle_leads.web.replay_store import load_cookies
    from circle_leads.scraper.member_api_reader import (
        MemberApiReader, SessionInvalid, ChallengeHit, fetch_space_posts,
    )
    from circle_leads.triage.pipeline import triage_records
    from circle_leads.discovery.discover_communities import community_slug_for_host

    started = time.time()
    # Stable slug for this host. NOT host.split(".")[0] -- that collapsed every
    # www.* host to "www" and every community.* host to "community", so
    # unrelated communities piled onto one row (see WORKLOG P10).
    slug = community_slug_for_host(host)
    cookies = {c["name"]: c["value"] for c in (load_cookies(db, host) or [])}
    state = ConnectionState.CONNECTED
    detail = ""
    total = readable = spaces_total = leads = 0
    partial = False
    try:
        reader = MemberApiReader(host, cookies=cookies,
                                 request_pause=0.2 if fast_pause else 0.7)
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
                        with_comments=with_comments, max_comment_posts=cap)
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
            detail = (f"{total} post(s), {leads} lead(s) from "
                      f"{readable}/{spaces_total} space(s){suffix}"
                      if total else
                      f"No readable posts ({spaces_total} space(s) visible){suffix}.")
    except SessionInvalid:
        state = ConnectionState.SESSION_EXPIRED
        detail = "Session rejected -- refresh the cookie."
    except ChallengeHit as exc:
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
        conn.spaces_readable = readable
        conn.spaces_total = spaces_total
        conn.last_sync_at = _dt.datetime.utcnow()
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
