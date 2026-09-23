"""Poll each community's newest-first feed and triage what is new.

One request per community per cycle:

    GET /internal_api/home_page_posts?sort=latest&page=1&per_page=N

with ``If-None-Match``, so an unchanged feed costs a 304 and no body. Anything
new goes through ``triage_records`` -- the same function the harvest calls --
so a post found here and the same post found by the harvest produce one stored
row and one lead, not two. That only holds because every reader flattens a post
with the same code (``scraper/public_reader.normalize_public_post``, which
prefers the full ``tiptap_body`` over Circle's 255-character preview).

What this deliberately does not do:

- It does not read comments. A hiring ask buried in a comment thread is the
  harvest's job; chasing it here would multiply the request count by the number
  of posts.
- It does not move the watermark on a failure. A 429 recorded as "no new posts"
  is how the old reader lost posts, and the watermark is the only thing
  standing between us and reading a community's whole archive again.
"""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import requests
from sqlalchemy import or_, select

from circle_leads.storage.database import Database
from circle_leads.storage.models import (
    Community,
    JoinStatus,
    ReplaySession,
    WatchMode,
    WatchState,
)

logger = logging.getLogger(__name__)

FEED_PATH = "/internal_api/home_page_posts?sort=latest&page={page}&per_page={per_page}"

# Five is enough to cover a burst between two checks on the busiest community
# we have (645 posts in 30 days, so ~1 per hour) while keeping a 304 cheap.
DEFAULT_PER_PAGE = 5
# Pages walked back when a burst arrives; past this we let the harvest catch
# the rest rather than spend a community's whole budget in one cycle.
MAX_PAGES = 5
# How many ids to remember. Circle can publish an id below the newest one when
# a draft is released, so "greater than the watermark" is not enough on its own.
RECENT_IDS_KEPT = 200

CHALLENGE_MARKERS = ("__cf_chl_", "cf-chl-", "<title>just a moment", "verifying you are a human")


@dataclass
class WatchOutcome:
    host: str
    status: str
    new_posts: int = 0
    leads: int = 0
    detail: str = ""
    # Seconds between the newest post's publication and this check finding it.
    lag_s: float | None = None


@dataclass
class WatchTuning:
    """How often each tier is polled, in seconds."""

    fast_interval: int = 120
    slow_interval: int = 900
    off_interval: int = 86_400
    # A community drops to the slow tier after this long without a post, and
    # returns to fast the moment it publishes one.
    quiet_days: int = 14
    per_page: int = DEFAULT_PER_PAGE
    max_pages: int = MAX_PAGES
    # Backoff after consecutive failures: 2 min, 4, 8 ... capped.
    max_backoff_s: int = 3_600
    jitter: float = 0.15
    tuning_note: str = field(default="", repr=False)


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _classify(resp: requests.Response) -> str:
    """One word for what Circle did, using the same vocabulary as the probe."""
    if resp.headers.get("cf-mitigated") == "challenge":
        return "challenge"
    body = resp.text[:4000].lower() if resp.content else ""
    if resp.status_code in (403, 503) and any(m in body for m in CHALLENGE_MARKERS):
        return "challenge"
    if resp.status_code == 429:
        return "ratelimited"
    if resp.status_code == 304:
        return "not_modified"
    if resp.status_code == 200:
        return "ok"
    if resp.status_code in (401, 403):
        return "unauthorized"
    if resp.status_code == 404:
        return "notfound"
    return f"http_{resp.status_code}"


def ensure_watch_rows(db: Database, *, quiet_days: int = 14) -> int:
    """Create a watch row for every community worth polling. Returns how many.

    Worth polling means: a Circle community with a host, that we either watch,
    have joined, or judged a fit. A row is never deleted here -- a community
    that stops answering is switched to ``off`` so the reason stays visible.

    A new row starts on the tier its own history earns. Starting everything on
    the fast tier looks harmless and is not: 173 communities every two minutes
    is 86 requests a minute, well past any budget we are willing to spend on
    one address, and the poller would simply fall behind on all of them
    instead of being on time for the ones that actually post.
    """
    from circle_leads.storage.models import Post

    created = 0
    cutoff = _now() - timedelta(days=quiet_days)
    with db.session() as s:
        rows = s.execute(
            select(Community.id, Community.host, Community.slug)
            .where(Community.platform == "circle")
            .where(Community.host.is_not(None))
            .where(
                or_(
                    Community.watching.is_(True),
                    Community.icp_flag.is_(True),
                    Community.relevant.is_(True),
                    Community.join_status == JoinStatus.JOINED.value,
                )
            )
        ).all()
        known = set(s.scalars(select(WatchState.community_id)).all())
        has_session = set(s.scalars(select(ReplaySession.host)).all())
        recently_posted = set(
            s.scalars(
                select(Post.community_id)
                .where(Post.content_type == "post")
                .where(Post.published_at > cutoff)
                .distinct()
            ).all()
        )
        for community_id, host, _slug in rows:
            if community_id in known:
                continue
            s.add(
                WatchState(
                    community_id=community_id,
                    host=host,
                    mode=WatchMode.ANON.value,
                    tier="fast" if community_id in recently_posted else "slow",
                    next_check_at=_now(),
                    recent_ids=[],
                )
            )
            created += 1
        # Nothing is read with a session it does not have.
        for row in s.scalars(select(WatchState).where(WatchState.mode == WatchMode.COOKIE.value)):
            if row.host not in has_session:
                row.mode = WatchMode.ANON.value
    return created


def due_states(db: Database, limit: int = 500) -> list[dict]:
    """Communities whose next check is due, soonest first, as plain dicts.

    Plain dicts because the network call happens outside the session: holding a
    Postgres connection open for the length of an HTTP request is how a pooler
    runs out of connections.
    """
    with db.session() as s:
        rows = s.scalars(
            select(WatchState)
            .where(WatchState.next_check_at <= _now())
            .order_by(WatchState.next_check_at.asc())
            .limit(limit)
        ).all()
        return [
            {
                "id": r.id,
                "community_id": r.community_id,
                "host": r.host,
                "mode": r.mode,
                "tier": r.tier,
                "last_post_id": r.last_post_id,
                "recent_ids": list(r.recent_ids or []),
                "etag": r.etag,
                "consecutive_errors": r.consecutive_errors,
            }
            for r in rows
        ]


def _next_delay(state: dict, status: str, tuning: WatchTuning) -> int:
    """Seconds until this community is checked again."""
    if status in ("ok", "not_modified"):
        base = tuning.fast_interval if state["tier"] == "fast" else tuning.slow_interval
    elif status in ("ratelimited", "challenge"):
        # The governor already paused this egress; do not also hammer the one
        # host that pushed back.
        base = min(tuning.max_backoff_s, 300 * (2 ** min(state["consecutive_errors"], 4)))
    elif status in ("notfound", "gone"):
        base = tuning.off_interval
    elif status == "unauthorized":
        base = tuning.slow_interval * 2
    else:
        base = min(tuning.max_backoff_s, tuning.fast_interval * (2 ** min(state["consecutive_errors"], 5)))
    return int(base * (1 + random.uniform(-tuning.jitter, tuning.jitter)))


def _fetch(session: requests.Session, host: str, page: int, tuning: WatchTuning,
           cookies: dict | None, etag: str | None) -> requests.Response:
    url = f"https://{host}" + FEED_PATH.format(page=page, per_page=tuning.per_page)
    headers = {"Accept": "application/json"}
    if etag and page == 1:
        headers["If-None-Match"] = etag
    return session.get(url, headers=headers, cookies=cookies or None, timeout=25,
                       allow_redirects=False)


def check_community(
    db: Database,
    state: dict,
    requirements,
    *,
    session: requests.Session,
    tuning: WatchTuning,
    use_llm: bool = False,
) -> WatchOutcome:
    """One feed check. Reads, triages anything new, then updates the row."""
    from circle_leads.scraper.public_reader import normalize_public_post
    from circle_leads.triage.pipeline import triage_records
    from circle_leads.web.replay_store import load_cookies

    host = state["host"]
    community_url = f"https://{host}"
    cookies = None
    if state["mode"] == WatchMode.COOKIE.value:
        cookies = {c["name"]: c["value"] for c in (load_cookies(db, host) or [])}
        if not cookies:
            return _finish(db, state, WatchOutcome(host, "unauthorized",
                                                   detail="no stored session"), tuning)

    seen = set(state["recent_ids"] or [])
    watermark = state["last_post_id"] or 0
    # Nothing stored yet means this community has never been polled. Its
    # archive already came in through the harvest, so the first pass only
    # writes the watermark; triaging page one here would re-classify old posts
    # and, for anything the harvest had not reached, send stale leads to Vini.
    seeding = watermark == 0 and not seen
    fresh: list[dict] = []
    seen_now: list[int] = []
    etag_value = state["etag"]
    newest_published: datetime | None = None

    for page in range(1, tuning.max_pages + 1):
        try:
            resp = _fetch(session, host, page, tuning, cookies, etag_value if page == 1 else None)
        except requests.RequestException as exc:
            return _finish(db, state, WatchOutcome(host, "error", detail=type(exc).__name__), tuning)

        status = _classify(resp)
        if status == "not_modified":
            return _finish(db, state, WatchOutcome(host, status), tuning)
        if status != "ok":
            return _finish(db, state, WatchOutcome(host, status,
                                                   detail=f"HTTP {resp.status_code}"), tuning)
        if page == 1:
            etag_value = resp.headers.get("etag") or etag_value

        try:
            data = resp.json() or {}
        except ValueError:
            return _finish(db, state, WatchOutcome(host, "error", detail="bad JSON"), tuning)
        records = data.get("records") or []
        if not records:
            break

        page_new = 0
        for rec in records:
            rid = rec.get("id")
            if rid is None or rid in seen_now:
                continue
            seen_now.append(rid)
            if rid in seen or rid <= watermark:
                continue
            page_new += 1
            if not seeding:
                fresh.append(rec)

        # Seeding only needs to know where the feed is now, and every record on
        # page one counts as new when there is no watermark yet -- so without
        # this it walks max_pages on every community and spends five times the
        # requests to learn one number.
        if seeding:
            break

        # A pinned post sits at the top of the feed forever, so a first page
        # that is entirely pinned says nothing about what is below it. That is
        # the only reason to look further when nothing new was found.
        all_pinned_first_page = page == 1 and all(
            r.get("pin_to_top") or r.get("pinned_at_top_of_space") for r in records
        )
        if not page_new and not all_pinned_first_page:
            break
        if not data.get("has_next_page"):
            break

    if seeding:
        return _finish(db, state, WatchOutcome(host, "ok", detail="watermark set"),
                       tuning, seen_ids=seen_now, etag=etag_value, seeded=True)

    if not fresh:
        return _finish(db, state, WatchOutcome(host, "ok"), tuning, etag=etag_value)

    # Oldest first, so the stored order matches how they were published.
    fresh.sort(key=lambda r: r.get("id") or 0)
    normalized = []
    for rec in fresh:
        norm = normalize_public_post(
            rec,
            community_url=community_url,
            excluded_content=getattr(requirements, "excluded_content", None),
            space_slug=rec.get("space_slug"),
        )
        if norm:
            normalized.append(norm)
            if norm.get("published_at"):
                published = norm["published_at"]
                if newest_published is None or published > newest_published:
                    newest_published = published

    leads = 0
    if normalized:
        slug = _community_slug(db, state["community_id"])
        res = triage_records(
            db, normalized, requirements,
            community=slug, source_url=community_url, use_llm=use_llm,
        )
        leads = len(res.leads)

    lag = None
    if newest_published is not None:
        lag = (_now() - newest_published).total_seconds()

    outcome = WatchOutcome(host, "ok", new_posts=len(normalized), leads=leads, lag_s=lag)
    return _finish(db, state, outcome, tuning,
                   seen_ids=[r.get("id") for r in fresh], etag=etag_value)


def _community_slug(db: Database, community_id: int) -> str:
    """The slug the harvest would use, so both paths name one community."""
    with db.session() as s:
        slug = s.scalar(select(Community.slug).where(Community.id == community_id))
    return slug or "watch"


def _finish(db: Database, state: dict, outcome: WatchOutcome, tuning: WatchTuning,
            *, seen_ids: list | None = None, etag: str | None = None,
            seeded: bool = False) -> WatchOutcome:
    """Write the result back. The watermark moves only on a successful read."""
    now = _now()
    with db.session() as s:
        row = s.get(WatchState, state["id"])
        if row is None:
            return outcome
        row.last_checked_at = now
        row.last_status = outcome.status
        row.last_detail = outcome.detail or None

        if outcome.status in ("ok", "not_modified"):
            row.consecutive_errors = 0
        else:
            row.consecutive_errors = (row.consecutive_errors or 0) + 1

        if etag is not None:
            row.etag = etag

        if seen_ids:
            ids = [i for i in seen_ids if i is not None]
            keep = list(row.recent_ids or []) + ids
            row.recent_ids = keep[-RECENT_IDS_KEPT:]
            row.last_post_id = max([row.last_post_id or 0] + ids)
            if not seeded:
                row.posts_seen = (row.posts_seen or 0) + outcome.new_posts
                row.leads_found = (row.leads_found or 0) + outcome.leads
                row.last_new_at = now
                row.tier = "fast"
        elif outcome.status in ("ok", "not_modified"):
            quiet_since = row.last_new_at or row.created_at or now
            if now - quiet_since > timedelta(days=tuning.quiet_days):
                row.tier = "slow"

        # A feed that refuses us anonymously may still answer with the session
        # we already hold; one that refuses both is not worth a fast cycle.
        if outcome.status == "unauthorized" and row.mode == WatchMode.ANON.value:
            has_session = s.scalar(
                select(ReplaySession.id).where(ReplaySession.host == row.host)
            )
            row.mode = WatchMode.COOKIE.value if has_session else WatchMode.OFF.value
        elif outcome.status == "notfound":
            row.mode = WatchMode.OFF.value

        delay = tuning.off_interval if row.mode == WatchMode.OFF.value else _next_delay(
            {**state, "consecutive_errors": row.consecutive_errors, "tier": row.tier},
            outcome.status, tuning,
        )
        row.next_check_at = now + timedelta(seconds=delay)
    return outcome


def run_watch(
    db: Database,
    *,
    tuning: WatchTuning | None = None,
    use_llm: bool = False,
    once: bool = False,
    on_outcome=None,
    stop=None,
) -> None:
    """Poll due communities forever (or one pass with ``once``).

    Requests go through the governed session, so the shared budget -- not this
    loop -- decides how fast we are actually allowed to ask.
    """
    from circle_leads.scraper.http_client import governed_session
    from circle_leads.storage.settings_store import load_effective_requirements

    tuning = tuning or WatchTuning()
    session = governed_session()
    session.headers.setdefault(
        "User-Agent",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    )
    requirements = load_effective_requirements(db)
    reloaded_at = time.monotonic()

    while True:
        if stop is not None and stop():
            return
        # A dashboard edit (say, the max post age) should reach the poller
        # without a restart, but re-reading it every cycle is a query per cycle.
        if time.monotonic() - reloaded_at > 300:
            requirements = load_effective_requirements(db)
            reloaded_at = time.monotonic()

        batch = due_states(db)
        if not batch:
            if once:
                return
            time.sleep(5)
            continue

        for state in batch:
            if stop is not None and stop():
                return
            try:
                outcome = check_community(
                    db, state, requirements,
                    session=session, tuning=tuning, use_llm=use_llm,
                )
            except Exception as exc:  # noqa: BLE001 - one bad community must not stop the loop
                logger.exception("watch failed for %s", state["host"])
                outcome = _finish(db, state,
                                  WatchOutcome(state["host"], "error", detail=str(exc)[:200]),
                                  tuning)
            if on_outcome is not None:
                on_outcome(outcome)
            elif outcome.status != "not_modified":
                logger.info(
                    "watch %s: %s%s", outcome.host, outcome.status,
                    f" +{outcome.new_posts} post(s), {outcome.leads} lead(s)"
                    + (f", lag {outcome.lag_s:.0f}s" if outcome.lag_s else "")
                    if outcome.new_posts else "",
                )
        if once:
            return
