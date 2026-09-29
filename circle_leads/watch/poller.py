"""Poll each community's newest-first feed and triage what is new.

One request per community per cycle:

    GET /internal_api/home_page_posts?sort=latest&page=1&per_page=N

with ``If-None-Match``, so an unchanged feed costs a 304 and no body. Anything
new goes through ``triage_records`` -- the same function the harvest calls --
so a post found here and the same post found by the harvest produce one stored
row and one lead, not two. That only holds because every reader flattens a post
with the same code (``scraper/public_reader.normalize_public_post``, which
prefers the full ``tiptap_body`` over Circle's 255-character preview).

Some communities refuse that feed to a member whose session reads their spaces
fine. Those members are read space by space instead (``_check_member_spaces``):
one request per space the session opens.

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
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import requests
from sqlalchemy import and_, or_, select

from circle_leads.reach import has_session as session_stored, readable, real_host
from circle_leads.storage.database import Database
from circle_leads.storage.heartbeat import start_heartbeat
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

# A member the home feed refuses is read space by space. Seen on 2026-09-29:
# onstartups, talentcollective, future-of-saas and engage.techsoup answered
# home_page_posts with 401 "You cannot perform this action." to sessions that
# read their spaces fine.
SPACES_PATH = "/internal_api/spaces"
SPACE_POSTS_PATH = "/internal_api/spaces/{space_id}/posts?sort=latest&page=1&per_page={per_page}"
# Which spaces open for a session is learnt by trying them, then trusted this
# long -- as long as the cookie scan waits before it reads everything again.
MEMBER_SPACES_TTL_S = 6 * 3600
# Spaces tried per learning pass, and read per check. TechSoup lists 222 spaces
# to a member who can read one of them.
MAX_SPACES_TRIED = 40
MAX_SPACES_READ = 8

# Failures a retry does not fix: the domain now answers with a redirect
# elsewhere, or Circle no longer serves a certificate for it. Nine such rows
# retried hourly for a week (2026-09-23..29) and filled the attention page.
# After PARK_AFTER in a row a row goes to the daily check (mode off), which
# switches it back on by itself if the feed ever answers again.
PERMANENT_FAILURES = ("moved", "tls_error")
PARK_AFTER = 5

CHALLENGE_MARKERS = ("__cf_chl_", "cf-chl-", "<title>just a moment", "verifying you are a human")


# How often the running poller re-reads which communities are worth watching.
# The list used to be built only by `watch --sync`, which the service does not
# pass, so every community the discovery and ICP stages found after the last
# manual sync was invisible to the poller forever.
RESYNC_EVERY_S = 1800.0


@dataclass
class WatchOutcome:
    host: str
    status: str
    new_posts: int = 0
    leads: int = 0
    detail: str = ""
    # Seconds between the newest post's publication and this check finding it.
    lag_s: float | None = None
    # The leads themselves, so the loop can report them without re-reading the
    # database. Only ones that are not duplicates of a lead we already have.
    lead_payloads: list = field(default_factory=list)


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
    if resp.status_code in (301, 302, 303, 307, 308):
        return "moved"
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
            # The reading rule the harvest and the join bot share
            # (circle_leads/reach.py). Matching platform == "circle" alone
            # once left out eight ICP-fit communities found through Circle's
            # directory; matching ("circle", "discover") still left out every
            # row older than the platform column -- among them the seven
            # busiest communities we hold a session for. And no paid
            # community without a login.
            .where(
                or_(
                    and_(
                        readable(),
                        or_(
                            Community.watching.is_(True),
                            Community.icp_flag.is_(True),
                            Community.relevant.is_(True),
                            Community.join_status == JoinStatus.JOINED.value,
                        ),
                    ),
                    # A stored member session is proof enough: the extension
                    # and the join bot only store one for a Circle community
                    # we belong to. Exit Five (a custom domain, no platform
                    # recorded) had a session and no watch row at all.
                    and_(real_host(), session_stored()),
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
        members = _member_ids(s)
        for community_id, host, _slug in rows:
            if community_id in known:
                continue
            s.add(
                WatchState(
                    community_id=community_id,
                    host=host,
                    mode=WatchMode.ANON.value,
                    tier="fast" if community_id in recently_posted | members else "slow",
                    next_check_at=_now(),
                    recent_ids=[],
                )
            )
            created += 1
        # A community we belong to stays on the fast tier, whatever its
        # history. Rows made before this rule sat on the slow tier (or off)
        # for weeks after the bot had joined.
        for row in s.scalars(select(WatchState).where(WatchState.community_id.in_(members))):
            row.tier = "fast"
            if row.mode == WatchMode.OFF.value and row.host in has_session:
                row.mode = WatchMode.COOKIE.value
                row.next_check_at = _now()
        # Nothing is read with a session it does not have.
        for row in s.scalars(select(WatchState).where(WatchState.mode == WatchMode.COOKIE.value)):
            if row.host not in has_session:
                row.mode = WatchMode.ANON.value
    return created


def _member_ids(s) -> set[int]:
    """Communities we belong to: the bot joined, or a member session is stored."""
    return set(s.scalars(
        select(Community.id).where(
            or_(Community.join_status == JoinStatus.JOINED.value,
                and_(real_host(), session_stored()))
        )
    ).all())


def _is_member(s, row: WatchState) -> bool:
    community = s.get(Community, row.community_id)
    if community is not None and community.join_status == JoinStatus.JOINED.value:
        return True
    return s.scalar(select(ReplaySession.id).where(ReplaySession.host == row.host)) is not None


def watch_member_now(db: Database, host: str) -> int | None:
    """Put a community we just became a member of on the 2-minute tier, now.

    Called when the join bot gets in and when a session is stored (the
    extension, the dashboard). Waiting for the half-hourly resync cost up to
    30 minutes, and the resync alone put a community with no posts in our
    database on the 15-minute tier. Returns the community id.
    """
    from circle_leads.discovery.discover_communities import unique_slug_for_host
    from circle_leads.storage.database import get_or_create_community

    host = (host or "").strip().lower().strip(".")
    if not host:
        return None
    with db.session() as s:
        community = s.scalar(
            select(Community).where(Community.host == host).order_by(Community.id)
        ) or get_or_create_community(s, slug=unique_slug_for_host(host),
                                     url=f"https://{host}")
        community.watching = True
        has_session = s.scalar(
            select(ReplaySession.id).where(ReplaySession.host == host)) is not None
        row = s.scalar(select(WatchState).where(WatchState.community_id == community.id))
        if row is None:
            row = WatchState(community_id=community.id, host=host,
                             mode=WatchMode.ANON.value, recent_ids=[])
            s.add(row)
        if row.mode == WatchMode.OFF.value:
            # The anonymous feed already refused us; the session is what reads it.
            row.mode = WatchMode.COOKIE.value if has_session else WatchMode.ANON.value
        row.host = host
        row.tier = "fast"
        row.consecutive_errors = 0
        row.next_check_at = _now()
        s.flush()
        return community.id


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
    from circle_leads.web.replay_store import load_cookies

    host = state["host"]
    cookies = None
    if state["mode"] == WatchMode.COOKIE.value:
        cookies = {c["name"]: c["value"] for c in (load_cookies(db, host) or [])}
        if not cookies:
            return _finish(db, state, WatchOutcome(host, "unauthorized",
                                                   detail="no stored session"), tuning)
        if _spaces_known(host):
            # The feed refused this member a few hours ago at most; asking it
            # again every two minutes only buys another 401.
            return _check_member_spaces(db, state, requirements, session=session,
                                        tuning=tuning, cookies=cookies, use_llm=use_llm)

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

    for page in range(1, tuning.max_pages + 1):
        try:
            resp = _fetch(session, host, page, tuning, cookies, etag_value if page == 1 else None)
        except requests.exceptions.SSLError:
            # Circle stops serving a certificate for a custom domain its owner
            # removed; the name still points at Circle, so this never heals.
            return _finish(db, state, WatchOutcome(host, "tls_error", detail="SSLError"), tuning)
        except requests.RequestException as exc:
            return _finish(db, state, WatchOutcome(host, "error", detail=type(exc).__name__), tuning)

        status = _classify(resp)
        if status == "not_modified":
            return _finish(db, state, WatchOutcome(host, status), tuning)
        if status == "unauthorized" and cookies:
            return _check_member_spaces(db, state, requirements, session=session,
                                        tuning=tuning, cookies=cookies, use_llm=use_llm)
        if status != "ok":
            return _finish(db, state, WatchOutcome(host, status, detail=_http_detail(resp)), tuning)
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

    return _take_new(db, state, requirements, fresh=fresh, seen_now=seen_now,
                     seeding=seeding, tuning=tuning, use_llm=use_llm,
                     member=cookies is not None, etag=etag_value)


def _http_detail(resp: requests.Response) -> str:
    """The status code, and for a redirect where it points: the new address
    is what a person needs to fix the row."""
    location = resp.headers.get("location") or resp.headers.get("Location")
    if resp.status_code in (301, 302, 303, 307, 308) and location:
        return f"HTTP {resp.status_code} -> {location[:200]}"
    return f"HTTP {resp.status_code}"


def _take_new(db: Database, state: dict, requirements, *, fresh: list[dict],
              seen_now: list, seeding: bool, tuning: WatchTuning, use_llm: bool,
              member: bool, etag: str | None = None, detail: str = "") -> WatchOutcome:
    """Triage what a check found new and move the watermark past it.

    The one place both readers -- the home feed and a member's spaces --
    store posts, so the watermark rules cannot drift apart between them.
    """
    from circle_leads.scraper.public_reader import normalize_public_post
    from circle_leads.triage.pipeline import triage_records

    host = state["host"]
    community_url = f"https://{host}"
    if seeding:
        return _finish(db, state, WatchOutcome(
            host, "ok", detail=f"{detail}; watermark set" if detail else "watermark set"),
            tuning, seen_ids=seen_now, etag=etag, seeded=True)

    if not fresh:
        return _finish(db, state, WatchOutcome(host, "ok", detail=detail), tuning, etag=etag)

    # Oldest first, so the stored order matches how they were published.
    fresh.sort(key=lambda r: r.get("id") or 0)
    normalized = []
    newest_published: datetime | None = None
    for rec in fresh:
        norm = normalize_public_post(
            rec,
            community_url=community_url,
            excluded_content=getattr(requirements, "excluded_content", None),
            space_slug=rec.get("space_slug"),
        )
        if norm:
            if member:
                # Read with a member's session, the way the cookie scan labels it.
                norm["permission_reference"] = "member_session"
            normalized.append(norm)
            if norm.get("published_at"):
                published = norm["published_at"]
                if newest_published is None or published > newest_published:
                    newest_published = published

    leads = 0
    lead_payloads: list = []
    if normalized:
        slug = _community_slug(db, state["community_id"])
        res = triage_records(
            db, normalized, requirements,
            community=slug, source_url=community_url, use_llm=use_llm,
        )
        leads = len(res.leads)
        lead_payloads = [dict(p, community=slug) for p in res.new_leads]

    lag = None
    if newest_published is not None:
        lag = (_now() - newest_published).total_seconds()

    outcome = WatchOutcome(host, "ok", new_posts=len(normalized), leads=leads, detail=detail,
                           lag_s=lag, lead_payloads=lead_payloads)
    return _finish(db, state, outcome, tuning,
                   seen_ids=[r.get("id") for r in fresh], etag=etag)


# --- members the home feed refuses ---------------------------------------------

@dataclass
class _MemberSpaces:
    """The spaces one session opens, learnt by trying them."""

    learned_at: float
    spaces: list[dict]
    listed: int


# host -> _MemberSpaces. Memory only: after a restart one refused feed request
# and one learning pass rebuild it.
_member_spaces: dict[str, _MemberSpaces] = {}


def _spaces_known(host: str) -> bool:
    known = _member_spaces.get(host)
    return known is not None and time.monotonic() - known.learned_at < MEMBER_SPACES_TTL_S


def _member_get(session: requests.Session, host: str, path: str,
                cookies: dict) -> requests.Response:
    return session.get(f"https://{host}{path}", headers={"Accept": "application/json"},
                       cookies=cookies, timeout=25, allow_redirects=False)


def _records(resp: requests.Response) -> list[dict] | None:
    """The records of a JSON list answer; None when it is not one."""
    try:
        data = resp.json()
    except ValueError:
        return None
    if isinstance(data, dict):
        data = data.get("records") or []
    if not isinstance(data, list):
        return None
    return [r for r in data if isinstance(r, dict)]


def _space_records(batch: list[dict], space: dict) -> list[dict]:
    return [dict(r, space_slug=r.get("space_slug") or space["slug"]) for r in batch]


def _slugs_we_have_read(db: Database, community_id: int) -> list[str]:
    """Space slugs in the links of the posts we hold, newest first. The cookie
    scan found those spaces readable, so a learning pass tries them first:
    tried in listed order, TechSoup's one readable space could sit past
    MAX_SPACES_TRIED."""
    from circle_leads.storage.models import Post

    with db.session() as s:
        urls = s.scalars(
            select(Post.url).where(Post.community_id == community_id, Post.url.like("%/c/%"))
            .order_by(Post.id.desc()).limit(500)
        ).all()
    slugs: list[str] = []
    for url in urls:
        match = re.search(r"/c/([^/?#]+)", url or "")
        if match and match.group(1) not in slugs:
            slugs.append(match.group(1))
    return slugs


def _learn_member_spaces(
    db: Database, state: dict, *, session: requests.Session, tuning: WatchTuning, cookies: dict,
) -> tuple[_MemberSpaces | None, list[dict], WatchOutcome | None]:
    """List the spaces the session sees and try each, the ones we have read first.

    Returns what opened and the records the tries fetched on the way (the
    first check reads nothing twice) -- or the outcome that stopped the pass.
    """
    host = state["host"]
    try:
        resp = _member_get(session, host, SPACES_PATH, cookies)
    except requests.RequestException as exc:
        return None, [], WatchOutcome(host, "error", detail=type(exc).__name__)
    status = _classify(resp)
    if status != "ok":
        detail = f"spaces list: {_http_detail(resp)}"
        if resp.status_code == 400:
            # "Please confirm before proceeding": the new-member profile step is open.
            return None, [], WatchOutcome(host, "unauthorized", detail=f"{detail} (profile step open)")
        return None, [], WatchOutcome(host, status, detail=detail)

    listed = [sp for sp in (_records(resp) or []) if sp.get("id") is not None]
    rank = {slug: i for i, slug in enumerate(_slugs_we_have_read(db, state["community_id"]))}
    listed.sort(key=lambda sp: rank.get(str(sp.get("slug") or ""), len(rank)))

    opened: list[dict] = []
    empty: list[dict] = []
    records: list[dict] = []
    for sp in listed[:MAX_SPACES_TRIED]:
        space = {"id": str(sp["id"]), "slug": str(sp.get("slug") or "")}
        if rank and (opened or empty) and space["slug"] not in rank:
            # The spaces we have posts from are open. One that opened only
            # lately shows up in the cookie scan within six hours; trying all
            # 222 of TechSoup's here for it is not worth the budget.
            break
        path = SPACE_POSTS_PATH.format(space_id=space["id"], per_page=tuning.per_page)
        try:
            resp = _member_get(session, host, path, cookies)
        except requests.RequestException:
            continue
        status = _classify(resp)
        if status in ("ratelimited", "challenge"):
            return None, [], WatchOutcome(
                host, status, detail=f"space {space['slug'] or space['id']}: {_http_detail(resp)}")
        batch = _records(resp) if status == "ok" else None
        if batch is None:
            continue
        # An empty space that opens is kept too, behind the busy ones: a
        # post there is as much a lead as anywhere else.
        (opened if batch else empty).append(space)
        records.extend(_space_records(batch, space))
        if len(opened) >= MAX_SPACES_READ:
            break

    learned = _MemberSpaces(time.monotonic(), (opened + empty)[:MAX_SPACES_READ], len(listed))
    _member_spaces[host] = learned
    return learned, records, None


def _check_member_spaces(db: Database, state: dict, requirements, *,
                         session: requests.Session, tuning: WatchTuning, cookies: dict,
                         use_llm: bool) -> WatchOutcome:
    """Read a member the home feed refuses: page one of each space the session
    opens, merged. Post ids are global on Circle, so one watermark still
    covers every space. A burst bigger than a page is left to the cookie scan."""
    host = state["host"]
    records: list[dict] | None = None
    if _spaces_known(host):
        learned = _member_spaces[host]
    else:
        learned, records, stopped = _learn_member_spaces(
            db, state, session=session, tuning=tuning, cookies=cookies)
        if stopped is not None:
            return _finish(db, state, stopped, tuning)

    if not learned.spaces:
        return _finish(db, state, WatchOutcome(
            host, "unauthorized",
            detail=f"feed refused; none of the {learned.listed} space(s) this session "
                   "sees opens"), tuning)

    if records is None:
        records = []
        for space in learned.spaces:
            path = SPACE_POSTS_PATH.format(space_id=space["id"], per_page=tuning.per_page)
            try:
                resp = _member_get(session, host, path, cookies)
            except requests.RequestException as exc:
                return _finish(db, state, WatchOutcome(host, "error", detail=type(exc).__name__),
                               tuning)
            status = _classify(resp)
            batch = _records(resp) if status == "ok" else None
            if batch is None:
                if status not in ("ratelimited", "challenge"):
                    # A space closed or the session died: learn afresh next
                    # time rather than guess which.
                    _member_spaces.pop(host, None)
                return _finish(db, state, WatchOutcome(
                    host, "error" if status == "ok" else status,
                    detail=f"space {space['slug'] or space['id']}: {_http_detail(resp)}"), tuning)
            records.extend(_space_records(batch, space))

    seen = set(state["recent_ids"] or [])
    watermark = state["last_post_id"] or 0
    seeding = watermark == 0 and not seen
    fresh: list[dict] = []
    seen_now: list = []
    for rec in records:
        rid = rec.get("id")
        if rid is None or rid in seen_now:
            continue
        seen_now.append(rid)
        if not seeding and rid not in seen and rid > watermark:
            fresh.append(rec)

    return _take_new(db, state, requirements, fresh=fresh, seen_now=seen_now, seeding=seeding,
                     tuning=tuning, use_llm=use_llm, member=True,
                     detail=f"read space by space: {len(learned.spaces)} of {learned.listed} "
                            "open to this session")


def _drain_unsynced_leads(db: Database) -> None:
    """Send leads the portal has not accepted yet.

    Best-effort: a missing credential or a portal error must not stop polling.
    The author-identity repair inside the drain runs once, then this only
    pushes whatever is still unsynced.
    """
    try:
        from circle_leads.export.vini_ingest import drain_unsynced_leads

        drain_unsynced_leads(db)
    except Exception:  # noqa: BLE001 - delivery must not stop the loop
        logger.exception("could not retry unsynced leads")


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
            if now - quiet_since > timedelta(days=tuning.quiet_days) and not _is_member(s, row):
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
        elif outcome.status in PERMANENT_FAILURES and row.consecutive_errors >= PARK_AFTER:
            row.mode = WatchMode.OFF.value
        elif outcome.status in ("ok", "not_modified") and row.mode == WatchMode.OFF.value:
            # Switched off because it refused us, and now the feed answers
            # without a login: the community opened up. Left off, it stayed on
            # the once-a-day check for good while its posts were readable.
            row.mode = WatchMode.ANON.value

        delay = tuning.off_interval if row.mode == WatchMode.OFF.value else _next_delay(
            {**state, "consecutive_errors": row.consecutive_errors, "tier": row.tier},
            outcome.status, tuning,
        )
        row.next_check_at = now + timedelta(seconds=delay)
    return outcome


def _report_leads(outcome: WatchOutcome) -> None:
    """Send each new lead to Telegram. Never raises: a lead is already saved
    and already on its way to Vini by the time we get here, so a messaging
    failure must not look like a pipeline failure."""
    from circle_leads.notify import escape, send

    for lead in outcome.lead_payloads:
        role = lead.get("job_title") or lead.get("hire_target") or "роль не указана"
        parts = [f"\U0001f4e5 <b>{escape(str(role))}</b>"]
        line = " · ".join(
            str(x) for x in (
                lead.get("community"),
                lead.get("budget"),
                lead.get("location"),
                f"score {lead['lead_score']}" if lead.get("lead_score") is not None else None,
            ) if x
        )
        if line:
            parts.append(escape(line))
        quote = (lead.get("evidence_quote") or "").strip()
        if quote:
            parts.append(f"<blockquote>{escape(quote[:300])}</blockquote>")
        url = lead.get("url")
        if url:
            parts.append(f'<a href="{escape(str(url))}">открыть пост</a>')
        if outcome.lag_s:
            parts.append(escape(f"найдено через {outcome.lag_s:.0f} с после публикации"))
        try:
            send("\n".join(parts), dedup_key=f"lead:{url or role}:{lead.get('community')}")
        except Exception:  # noqa: BLE001 - reporting must not break the loop
            logger.exception("telegram: reporting a lead failed")


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

    # Beside the work, not inside it: a batch of overdue communities can take
    # longer than the watchdog's patience, and a busy poller that looks dead
    # trains the person to ignore the alert channel.
    cancel_beat = start_heartbeat(db, "watcher_heartbeat",
                                  runtime_key="watcher_runtime", service="watcher")
    try:
        _run_watch_loop(db, session, requirements, reloaded_at, tuning,
                        use_llm=use_llm, once=once, stop=stop, on_outcome=on_outcome)
    finally:
        cancel_beat()


def _run_watch_loop(db: Database, session, requirements, reloaded_at, tuning,
                    *, use_llm: bool, once: bool, stop, on_outcome) -> None:
    # Imported here, not inherited from run_watch: this used to be one function
    # and the import was local to it. Splitting the loop out left this call
    # site looking at a name that does not exist in its own scope, and because
    # it only runs once the 300s reload timer expires, the service started
    # cleanly and died five minutes later, every five minutes.
    from circle_leads.storage.settings_store import load_effective_requirements

    # Sync on the first pass, then on a timer. Without this the watch list is
    # whatever the last `watch --sync` built: the service does not pass that
    # flag, so discovery and the ICP pass fed a list the poller never re-read.
    synced_at = 0.0
    while True:
        if stop is not None and stop():
            return
        # A dashboard edit (say, the max post age) should reach the poller
        # without a restart, but re-reading it every cycle is a query per cycle.
        if time.monotonic() - reloaded_at > 300:
            requirements = load_effective_requirements(db)
            reloaded_at = time.monotonic()

        if time.monotonic() - synced_at > RESYNC_EVERY_S:
            synced_at = time.monotonic()
            try:
                added = ensure_watch_rows(db, quiet_days=tuning.quiet_days)
                if added:
                    logger.info("watch list: %d community(ies) added", added)
            except Exception:  # noqa: BLE001 - a failed sync must not stop polling
                logger.exception("could not refresh the watch list")
            _drain_unsynced_leads(db)

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
            if outcome.lead_payloads:
                _report_leads(outcome)
            if on_outcome is not None:
                on_outcome(outcome)
            elif outcome.status != "not_modified":
                logger.info(
                    "watch %s: %s%s", outcome.host, outcome.status,
                    f" +{outcome.new_posts} post(s), {outcome.leads} lead(s)"
                    + (f", lag {outcome.lag_s:.0f}s" if outcome.lag_s else "")
                    if outcome.new_posts else "",
                )
        _drain_unsynced_leads(db)
        if once:
            return
