"""End-to-end automatic harvest: discover public communities, then read them.

One command chains the pieces built separately:

    search niches (Exa)  ->  persist finds  ->  for each community that has
    public spaces, read them, classify, file leads.

Designed to run on a schedule (twice a day). It only reads *public* spaces --
those a logged-out visitor can already see -- so it needs no login, cookie, or
membership. Private spaces return 401/403 and are skipped. Re-runs are cheap:
posts are de-duplicated, and communities are re-checked for new public content
rather than re-processed from scratch.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from sqlalchemy import func, nullsfirst, or_, select

from circle_leads.config.settings import Requirements
from circle_leads.discovery.join_type import (
    fetch_join_classification,
    refine_join_classification,
)
from circle_leads.discovery.persist import persist_finds
from circle_leads.discovery.validate_finds import (
    is_interstitial_title,
    is_subdomain_community,
)
from circle_leads.discovery.web_search import discover_by_search
from circle_leads.reach import ANSWERED_AS_CIRCLE, CIRCLE_PLATFORMS, readable
from circle_leads.scraper.public_reader import PublicReader, discover_and_read_public
from circle_leads.storage.activity import log_activity
from circle_leads.storage.database import Database
from circle_leads.storage.models import Community, Post
from circle_leads.triage.pipeline import triage_records

logger = logging.getLogger(__name__)

# Default niches, used when none are given. Editable via the CLI.
DEFAULT_NICHES = [
    "software developer",
    "AI builder",
    "startup founder",
    "indie hacker",
    "SaaS founder",
]


# Circle budgets requests per IP, so re-reading a community that never has
# anything new is paid for by the ones that do. The re-check interval follows
# what the last read actually saw (Community.read_outcome), never the post
# count alone -- "no posts" also covers empty hiring spaces, posts past the age
# cap, and a read that simply failed:
#   private / gone      -> CLOSED_RECHECK_HOURS
#   public, a post in the last ACTIVE_DAYS -> the caller's min_recheck_hours
#   public, nothing that recent            -> QUIET_RECHECK_HOURS
#   error, or not recorded yet             -> min_recheck_hours (as before)
ACTIVE_DAYS = 30
QUIET_RECHECK_HOURS = 72.0
CLOSED_RECHECK_HOURS = 168.0

OUTCOME_PUBLIC = "public"
OUTCOME_PRIVATE = "private"
OUTCOME_GONE = "gone"
OUTCOME_ERROR = "error"
# join_type and the display name change rarely; one communities/current call
# a week per community is enough, unless the name is still missing.
JOIN_RECHECK_DAYS = 7


@dataclass
class _ReadState:
    join_type: str | None = None
    join_checked_at: datetime | None = None
    name: str | None = None
    newest_post_at: datetime | None = None
    read_outcome: str | None = None


def _read_state(db: Database, slug: str) -> _ReadState:
    with db.session() as s:
        c = s.scalar(select(Community).where(Community.slug == slug))
        if c is None:
            return _ReadState()
        newest = s.scalar(
            select(func.max(Post.published_at)).where(Post.community_id == c.id)
        )
        return _ReadState(
            c.join_type, c.join_type_checked_at, c.name, newest, c.read_outcome
        )


def _recheck_hours(base_hours: float, watching: bool, state: _ReadState, now: datetime) -> float:
    """How long to leave a community alone after reading it."""
    if watching:
        return base_hours
    if state.read_outcome in (OUTCOME_PRIVATE, OUTCOME_GONE):
        return max(base_hours, CLOSED_RECHECK_HOURS)
    if state.read_outcome == OUTCOME_PUBLIC:
        recent = state.newest_post_at is not None and now - state.newest_post_at <= timedelta(days=ACTIVE_DAYS)
        return base_hours if recent else max(base_hours, QUIET_RECHECK_HOURS)
    return base_hours  # error, or read before read_outcome existed


def _list_outcome(status: int | None, has_spaces: bool, join) -> str:
    """Classify the space-list response of one read."""
    if join is not None and (join.detail or "").startswith("host no longer maps"):
        return OUTCOME_GONE
    if has_spaces or status == 200:
        return OUTCOME_PUBLIC
    if status in (401, 403):
        return OUTCOME_PRIVATE
    if status == 404:
        return OUTCOME_GONE
    if status is None:
        return OUTCOME_PRIVATE  # a reader that doesn't report status (stubs)
    return OUTCOME_ERROR  # 0 (network), 429, 5xx, anything odd


def _needs_name(name: str | None, slug: str) -> bool:
    return not name or name == slug or is_interstitial_title(name)


@dataclass
class HarvestResult:
    niches: list[str] = field(default_factory=list)
    new_communities: int = 0
    communities_read: int = 0
    public_spaces: int = 0
    posts_read: int = 0
    leads_found: int = 0
    errors: list[str] = field(default_factory=list)


def _reads_on_circle(c: Community) -> bool:
    """True when the public reader can read this community.

    Any Circle-hosted community works -- a ``<slug>.circle.so`` subdomain or a
    custom domain like ``forum.joelpilger.com``, including one found through
    Circle's own directory (platform "discover", which _community_hosts only
    passes with a real address). Rows created before the ``platform`` column
    existed carry NULL: a subdomain, or a custom domain whose join-type check
    was answered by Circle. community.freelancemvp.com, free and ICP-fit, was
    skipped on URL shape alone and never read.
    """
    if c.platform is not None:
        return c.platform in CIRCLE_PLATFORMS
    return is_subdomain_community(c.url) or c.join_type in ANSWERED_AS_CIRCLE


def _community_hosts(
    db: Database, *, limit: int, watched_only: bool = False
) -> list[tuple[str, str, object, bool]]:
    """Return (host, slug, last_synced_at, watching) for readable communities.

    Watched ("subscribed") communities come first, then ICP-fit ones, so the
    fast lane services subscriptions before sweeping others and the sweep
    spends its budget on communities we actually want. With ``watched_only``
    the sweep is dropped entirely -- only the watchlist. The last-synced time
    is the incremental watermark: a previously-read community is only re-read
    for posts newer than that.

    Ordering is the whole point of this function, and it used to get two things
    wrong:

    * It sorted by ``relevance_score`` -- the older web-search ranking from
      discovery/finder.py -- and never looked at ``icp_flag``. So the dashboard
      could show a deep "queued for the scraper" backlog while the scraper read
      a completely unrelated set of communities. The ICP queue fed nothing.
    * It sorted by score alone, which is a *fixed* key: the same top rows won
      every run forever. Measured in prod: 195 rows permanently occupied a
      150-row head, 0 communities outside it had ever been synced, and none of
      the 10,189 file-imported rows had ever been read once.

    Sorting by ``last_synced_at`` ascending with NULLs first fixes the second:
    never-read communities go to the front, and reading one sends it to the
    back, so the head rotates instead of setting. Score is only a tiebreak now.
    """
    with db.session() as s:
        # Narrow in SQL to what _reads_on_circle can possibly accept (NULL
        # platform still needs the per-row fallback, so it stays in). Without
        # this the whole table is materialised just to discard ~15% of it in
        # Python, which gets worse every time discovery runs. readable() is the
        # rule the join bot and the dashboard share (circle_leads/reach.py): a
        # real address, and no paid community we hold no login for.
        stmt = (
            select(Community)
            .where(readable())
            .order_by(
                Community.watching.desc(),
                Community.icp_flag.desc(),
                nullsfirst(Community.last_synced_at.asc()),
                Community.icp_score.desc(),
            )
        )
        rows = list(s.scalars(stmt).all())
        out = []
        for c in rows:
            if not _reads_on_circle(c):
                continue  # Discover listings / FB / Slack / dead hosts can't be read
            if watched_only and not c.watching:
                continue
            # A stored url is sometimes a deep link, not the community's origin
            # (a circle_directory-resolved join_url can be a /checkout/... or
            # /join?invitation_token=... page) -- a naive scheme-strip left that
            # path glued onto every API call built from it (PublicReader's own
            # base, and fetch_join_classification below), breaking both against
            # a nonsense URL. urlparse().hostname discards the path/query.
            host = c.host or urlparse(c.url).hostname or (
                c.url.replace("https://", "").replace("http://", "").strip("/"))
            out.append((host, c.slug, c.last_synced_at, bool(c.watching)))
            if len(out) >= limit:
                break
        return out


def _niches_from_config(requirements) -> list[str]:
    """Build search niches from the config's target roles (and a few skills).

    So the roles you set in the dashboard's Config tab actually drive
    discovery. Roles like "Backend Developer" become good search phrases;
    a couple of skills are appended for breadth. Falls back to DEFAULT_NICHES.
    """
    roles = [r for r in (requirements.target_roles or []) if r.strip()]
    skills = [s for s in (requirements.target_skills or []) if s.strip()]
    # All roles are searched (they're the primary signal); a few skill angles
    # are appended. The total is capped by max_search_niches to bound Exa cost;
    # 0 means no cap (search everything).
    niches: list[str] = list(roles)
    for sk in skills[:3]:
        niches.append(f"{sk} developer")
    cap = getattr(requirements, "max_search_niches", 12)
    if cap and cap > 0:
        niches = niches[:cap]
    niches = niches or list(DEFAULT_NICHES)

    # Optionally broaden with an LLM so discovery isn't limited to the exact
    # terms typed -- related roles, skills, and community types a hirer gathers
    # in. Gated by config; degrades to the seeds when no key is available.
    if getattr(requirements, "expand_search_with_ai", False):
        try:
            from circle_leads.discovery.query_expander import expand_niches

            max_total = getattr(requirements, "max_expanded_niches", 25) or 25
            extra = max(0, max_total - len(niches))
            if extra > 0:
                niches = expand_niches(niches, max_extra=extra)[:max_total]
        except Exception:  # noqa: BLE001 - expansion is best-effort
            pass
    return niches


def _existing_community_urls(db: Database, *, limit: int) -> list[str]:
    """Top existing community subdomains, to seed findSimilar expansion."""
    with db.session() as s:
        rows = s.scalars(
            select(Community).order_by(Community.relevance_score.desc()).limit(limit * 3)
        ).all()
        out = []
        for c in rows:
            if is_subdomain_community(c.url):
                out.append(c.url)
            if len(out) >= limit:
                break
        return out


def harvest(
    db: Database,
    requirements: Requirements,
    *,
    niches: list[str] | None = None,
    search: bool = True,
    only_new: bool = False,
    max_communities: int = 150,
    max_pages: int = 5,
    use_llm: bool = False,
    min_score: int = 20,
    verbose_log: bool = False,
    include_comments: bool = False,
    recency_days: int = 30,
    all_spaces: bool = False,
    min_recheck_hours: float = 6.0,
    force_recheck: bool = False,
    watched_only: bool = False,
) -> HarvestResult:
    """Discover public communities and read their public spaces for leads.

    Behaviour:
    - ``search`` (default True) discovers new communities; set False to only
      re-read known ones.
    - Known communities are re-read every run for *new* posts. Each community's
      watermark is the later of its last-read time and the ``recency_days``
      window, so old history is not re-fetched.
    - ``only_new`` (default False) reads only communities never read before --
      use it to focus a run purely on fresh discoveries.
    - ``recency_days`` bounds how far back to look for a never-read community.
    - ``max_communities`` caps how many readable communities a single run
      touches (watched first, then by score). Set high enough to cover the
      whole readable set -- a lower cap permanently hides the tail, since the
      order is stable and the same head is picked every run. Re-reads are
      cheap (incremental watermark), so the cost of a high cap is one slow
      first pass per community, not repeated work.
    """
    niches = niches or _niches_from_config(requirements)
    result = HarvestResult(niches=niches)

    # --- 1. Discover new communities via search --------------------------
    if search:
        # Seed findSimilar with the top communities we already have, so a dry
        # keyword search still expands from your existing set.
        seed_urls = _existing_community_urls(db, limit=8)
        for niche in niches:
            try:
                disc = discover_by_search(niche, expand_from=seed_urls)
                res = persist_finds(
                    db, disc.ranked, niche=niche, min_score=min_score,
                    source="harvest",
                )
                result.new_communities += res.new_count
            except Exception as exc:  # noqa: BLE001
                result.errors.append(f"search '{niche}': {exc.__class__.__name__}")

    # --- 2. Read every readable community's public spaces ----------------
    recency_cutoff = (
        datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=recency_days)
    )
    # Hard age cap: never read past this, even on a first full-backlog pass.
    # Keeps the harvest from paginating years deep and surfacing stale leads.
    age_cutoff = None
    if getattr(requirements, "max_post_age_days", 0) and requirements.max_post_age_days > 0:
        age_cutoff = (
            datetime.now(timezone.utc).replace(tzinfo=None)
            - timedelta(days=requirements.max_post_age_days)
        )
    hosts = _community_hosts(db, limit=max_communities, watched_only=watched_only)
    for host, slug, last_synced, watching in hosts:
        if only_new and last_synced is not None:
            continue  # caller asked to read only never-seen communities
        # A discover.circle.so row is a directory listing, not a community:
        # its "spaces" are Circle's own marketplace, never this community's.
        if host.lower().endswith("discover.circle.so"):
            continue
        state = _read_state(db, slug)
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        # Skip a community we re-checked recently: re-reading it this soon
        # would spend Circle's per-IP budget to fetch nothing new. Quiet and
        # silent communities wait longer (see _recheck_hours). force_recheck
        # (the dashboard "re-check" box) overrides this.
        wait_hours = _recheck_hours(min_recheck_hours, watching, state, now)
        if (
            not force_recheck
            and last_synced is not None
            and (now - last_synced) < timedelta(hours=wait_hours)
        ):
            with db.session() as s:
                log_activity(
                    s, kind="read", level="info", community=slug,
                    summary=(
                        f"{host}: checked {_ago(last_synced)} ago — skipping "
                        f"(re-check interval {wait_hours:g}h)"
                    ),
                    detail={"last_synced": last_synced.isoformat(),
                            "min_recheck_hours": wait_hours},
                )
            continue
        # First read of a community: pull its whole backlog (bounded by
        # max_pages), because its best hiring posts are often months old and a
        # tight recency window would skip them. Only on *re-reads* do we apply
        # the incremental watermark, so we don't re-fetch history every run.
        first_read = last_synced is None
        if first_read:
            since = None  # never read before -> read everything available
        else:
            since = last_synced if last_synced > recency_cutoff else recency_cutoff
        # The age cap wins over both: no post older than it is ever worth reading.
        if age_cutoff is not None and (since is None or since < age_cutoff):
            since = age_cutoff
        # Say what kind of read this is: a first full-backlog pass, or an
        # incremental re-read only pulling posts newer than the watermark.
        if first_read:
            scope = (
                "first read (full backlog)" if since is None
                else f"first read (back to {since.date()})"
            )
        else:
            scope = f"re-read (posts since {since.date()})"
        with db.session() as s:
            log_activity(
                s, kind="read", community=slug,
                summary=f"Checking {host} for public spaces — {scope}",
                detail={"host": host, "scope": scope,
                        "since": since.isoformat() if since else "all"},
            )
        try:
            reader = PublicReader(host)
            spaces = reader.list_spaces()
        except Exception as exc:  # noqa: BLE001
            result.errors.append(f"read '{host}': {exc.__class__.__name__}")
            with db.session() as s:
                log_activity(s, kind="read", level="error", community=slug,
                             summary=f"Failed to reach {host}: {exc.__class__.__name__}")
            continue

        # Classify how this community can be joined (free/paid/invite-only),
        # independent of whether its space list is public -- a fully private
        # community (empty space list, below) is exactly the case join_type
        # most needs to explain. One public JSON call, no cookie, which also
        # carries the display name; repeated weekly (pricing does change), or
        # sooner while the name is still missing.
        join = None
        join_due = (
            force_recheck
            or state.join_checked_at is None
            or now - state.join_checked_at > timedelta(days=JOIN_RECHECK_DAYS)
            or _needs_name(state.name, slug)
        )
        if join_due:
            try:
                join = fetch_join_classification(host, session=reader.session)
            except Exception:  # noqa: BLE001 - classification must never fail the read
                join = None
        if join is not None:
            with db.session() as s:
                c = s.scalar(select(Community).where(Community.slug == slug))
                if c is not None:
                    # A private community answers 401 here on every read;
                    # a manual verdict or the listing's price must survive
                    # that, as they do in classify-join-types.
                    join = refine_join_classification(join, c)
                    c.join_type = join.join_type
                    c.join_type_detail = join.detail[:2000]
                    c.join_type_checked_at = datetime.now(timezone.utc).replace(tzinfo=None)

        outcome = _list_outcome(getattr(reader, "last_status", None), bool(spaces), join)
        if not spaces:
            what = {
                OUTCOME_PRIVATE: "no public space list (fully private)",
                OUTCOME_GONE: "community not found",
                OUTCOME_PUBLIC: "space list is empty",
            }.get(outcome, f"space list failed (HTTP {getattr(reader, 'last_status', '?')}) — will retry")
            with db.session() as s:
                log_activity(
                    s, kind="read", community=slug,
                    summary=(f"{host}: {what} "
                             f"[join: {join.join_type if join else state.join_type or 'unknown'}]"),
                )
            _mark_synced(db, slug, outcome)
            continue

        # Set/repair the community's real display name from the JSON API. This
        # avoids the marketing HTML page, which a datacenter IP gets served as a
        # "Verifying you are a human" bot-check -- so the name never gets that
        # junk. Only fills a missing/interstitial name; a good name is left be.
        # The join-type call above already fetched it; ask again only if that
        # call didn't run or failed.
        real_name = join.name if join is not None else None
        if real_name is None and join is None and _needs_name(state.name, slug):
            try:
                real_name = reader.community_name()
            except Exception:  # noqa: BLE001 - name is cosmetic; never fail the read
                real_name = None
        if real_name:
            with db.session() as s:
                c = s.scalar(select(Community).where(Community.slug == slug))
                if c is not None and (not c.name or c.name == slug
                                      or is_interstitial_title(c.name)):
                    c.name = real_name

        # Read each public lead-space, logging what is checked and read.
        records = []
        public_count = 0
        from circle_leads.scraper.public_reader import normalize_public_post
        targets = spaces if all_spaces else _lead_spaces(spaces)
        with db.session() as s:
            log_activity(
                s, kind="read", community=slug,
                summary=(
                    f"{host}: {len(spaces)} space(s) found, reading "
                    f"{len(targets)} {'(all)' if all_spaces else 'hiring-related'}"
                ),
                detail={"spaces_total": len(spaces),
                        "spaces_to_read": len(targets),
                        "reading": ", ".join(sp.name for sp in targets[:12])},
            )
        for sp in targets:
            # Guard each space: a malformed post or a classify error must not
            # abort the whole run and lose every community not yet processed.
            try:
                # Read deeper on a first pass; a light re-read (incremental
                # watermark) only needs the top few pages. read_space stops
                # paging as soon as it crosses `since`, so the age cap still
                # bounds the depth.
                pages = max(max_pages, 10) if first_read else max_pages
                was_public, raw = reader.read_space(sp.id, max_pages=pages, since=since)
                sp.is_public = was_public
                if not was_public:
                    with db.session() as s:
                        log_activity(s, kind="read", community=slug, space=sp.name,
                                     summary=f"{host} / {sp.name}: private, skipped")
                    continue
                public_count += 1
                space_recs = [
                    r for r in (
                        normalize_public_post(x, community_url=reader.base,
                                              excluded_content=requirements.excluded_content,
                                              space_slug=sp.slug)
                        for x in raw
                    ) if r
                ]
                # Optionally read comments too (a hiring ask can be a reply).
                if include_comments:
                    for raw_post in raw:
                        if raw_post.get("comments_count"):
                            for c in reader.read_comments(raw_post.get("id")):
                                crec = normalize_public_post(
                                    c, community_url=reader.base,
                                    excluded_content=requirements.excluded_content,
                                    space_slug=sp.slug)
                                if crec:
                                    crec["content_type"] = "comment"
                                    space_recs.append(crec)
                records.extend(space_recs)
                with db.session() as s:
                    log_activity(
                        s, kind="read", level="success", community=slug, space=sp.name,
                        summary=(
                            f"{host} / {sp.name}: read {len(space_recs)} public "
                            f"post(s) (up to {pages} page(s))"
                        ),
                        detail={"space": sp.name, "posts": len(space_recs),
                                "pages_scanned": pages,
                                "raw_fetched": len(raw)},
                        items_seen=len(space_recs),
                    )
            except Exception as exc:  # noqa: BLE001 - one bad space must not kill the run
                result.errors.append(f"{host}/{sp.name}: {exc.__class__.__name__}")
                with db.session() as s:
                    log_activity(s, kind="read", level="error", community=slug, space=sp.name,
                                 summary=f"{host} / {sp.name}: error {exc.__class__.__name__}, skipped")
                continue

        if not public_count:
            # The list answered, but every space refused us: closed in practice.
            _mark_synced(db, slug, OUTCOME_PRIVATE)
            continue

        result.communities_read += 1
        result.public_spaces += public_count
        result.posts_read += len(records)

        community_leads = 0
        already_seen = 0
        if records:
            triage_res = triage_records(
                db, records, requirements, community=slug,
                source_url=reader.base, use_llm=use_llm, verbose_log=verbose_log,
            )
            community_leads = len(triage_res.leads)
            already_seen = getattr(triage_res, "already_seen", 0)
            result.leads_found += community_leads
        # Per-community summary: how much was read here and how many leads it
        # produced -- so the activity feed shows results community by community,
        # not just one number at the very end.
        with db.session() as s:
            log_activity(
                s, kind="read",
                level="success" if community_leads else "info",
                community=slug,
                summary=(
                    f"{host}: {public_count} public space(s), {len(records)} "
                    f"post(s) read → {community_leads} lead(s) saved"
                    + (f", {already_seen} seen before" if already_seen else "")
                ),
                detail={"host": host, "public_spaces": public_count,
                        "posts_read": len(records), "leads_saved": community_leads,
                        "already_seen": already_seen},
                items_seen=len(records),
                leads_found=community_leads,
            )
        _mark_synced(db, slug, OUTCOME_PUBLIC)

    with db.session() as s:
        log_activity(
            s,
            kind="discover",
            level="success" if result.leads_found else "info",
            summary=(
                f"Harvest: {result.new_communities} new communities, "
                f"read {result.communities_read} ({result.public_spaces} public "
                f"spaces, {result.posts_read} posts), {result.leads_found} lead(s)"
            ),
            detail={"niches": ", ".join(niches), "errors": "; ".join(result.errors[:5])},
            items_seen=result.posts_read,
            leads_found=result.leads_found,
        )

    _register_monitored_communities(db)
    return result


def _register_monitored_communities(db: Database) -> None:
    """Push every monitored community to the Warmr portal intake endpoint.

    Best-effort and env-gated: a no-op unless COMMUNITY_INTAKE_API_SECRET is
    set, and a portal outage must never fail a harvest.
    """
    try:
        from circle_leads.export.community_intake import push_monitored_communities

        res = push_monitored_communities(db)
        if res.sent or res.errors:
            with db.session() as s:
                log_activity(
                    s,
                    kind="export",
                    level="error" if res.errors else "success",
                    summary=(
                        f"Community intake: registered {res.sent} community/ies "
                        f"with the portal"
                        + (f" — {res.errors[0]}" if res.errors else "")
                    ),
                    detail={"sent": res.sent, "attempted": res.attempted,
                            "skipped": res.skipped,
                            "errors": "; ".join(res.errors[:3])},
                )
    except Exception as exc:  # noqa: BLE001 - the sync must never break a harvest
        logger.warning("Community intake push failed: %s", exc)


def _ago(when: datetime) -> str:
    """Human-readable 'time since' for a naive UTC datetime."""
    delta = datetime.now(timezone.utc).replace(tzinfo=None) - when
    mins = int(delta.total_seconds() // 60)
    if mins < 60:
        return f"{mins}m"
    hours = mins // 60
    if hours < 24:
        return f"{hours}h"
    return f"{hours // 24}d"


def _mark_synced(db: Database, slug: str, outcome: str) -> None:
    with db.session() as s:
        c = s.scalar(select(Community).where(Community.slug == slug))
        if c is not None:
            c.last_synced_at = datetime.now(timezone.utc).replace(tzinfo=None)
            c.read_outcome = outcome


def _lead_spaces(spaces):
    """Spaces whose name/slug suggests hiring or project requests, else all."""
    hints = ("consultant", "project", "job", "hir", "need", "opportunit",
             "collab", "client", "gig", "freelance", "request")
    lead = [s for s in spaces
            if any(h in (s.slug + " " + s.name).lower() for h in hints)]
    return lead or spaces
