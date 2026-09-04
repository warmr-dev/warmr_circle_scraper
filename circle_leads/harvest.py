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

from sqlalchemy import select

from circle_leads.config.settings import Requirements
from circle_leads.discovery.persist import persist_finds
from circle_leads.discovery.validate_finds import is_subdomain_community
from circle_leads.discovery.web_search import discover_by_search
from circle_leads.scraper.public_reader import PublicReader, discover_and_read_public
from circle_leads.storage.activity import log_activity
from circle_leads.storage.database import Database
from circle_leads.storage.models import Community
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


@dataclass
class HarvestResult:
    niches: list[str] = field(default_factory=list)
    new_communities: int = 0
    communities_read: int = 0
    public_spaces: int = 0
    posts_read: int = 0
    leads_found: int = 0
    errors: list[str] = field(default_factory=list)


def _community_hosts(db: Database, *, limit: int) -> list[tuple[str, str, object]]:
    """Return (host, slug, last_synced_at) for readable community subdomains.

    Every known community is returned -- both freshly discovered ones and ones
    read before. The last-synced time is the incremental watermark: a
    previously-read community is only re-read for posts newer than that.
    """
    with db.session() as s:
        stmt = select(Community).order_by(Community.relevance_score.desc())
        rows = list(s.scalars(stmt).all())
        out = []
        for c in rows:
            if not is_subdomain_community(c.url):
                continue  # Discover listings can't be read; only subdomains
            host = c.url.replace("https://", "").replace("http://", "").strip("/")
            out.append((host, c.slug, c.last_synced_at))
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
    return niches or DEFAULT_NICHES


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
    max_communities: int = 40,
    max_pages: int = 5,
    use_llm: bool = False,
    min_score: int = 20,
    verbose_log: bool = False,
    include_comments: bool = False,
    recency_days: int = 30,
    all_spaces: bool = False,
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
    hosts = _community_hosts(db, limit=max_communities)
    for host, slug, last_synced in hosts:
        if only_new and last_synced is not None:
            continue  # caller asked to read only never-seen communities
        # Only fetch posts newer than the recency window, and newer than the
        # last time this community was read (whichever is later). A never-read
        # community uses the recency window alone.
        since = recency_cutoff
        if last_synced is not None and last_synced > recency_cutoff:
            since = last_synced
        with db.session() as s:
            log_activity(s, kind="read", community=slug,
                         summary=f"Checking community {host} for public spaces")
        try:
            reader = PublicReader(host)
            spaces = reader.list_spaces()
        except Exception as exc:  # noqa: BLE001
            result.errors.append(f"read '{host}': {exc.__class__.__name__}")
            with db.session() as s:
                log_activity(s, kind="read", level="error", community=slug,
                             summary=f"Failed to reach {host}: {exc.__class__.__name__}")
            continue

        if not spaces:
            with db.session() as s:
                log_activity(s, kind="read", community=slug,
                             summary=f"{host}: no public space list (fully private)")
            _mark_synced(db, slug)
            continue

        # Read each public lead-space, logging what is checked and read.
        records = []
        public_count = 0
        from circle_leads.scraper.public_reader import normalize_public_post
        for sp in (spaces if all_spaces else _lead_spaces(spaces)):
            # Guard each space: a malformed post or a classify error must not
            # abort the whole run and lose every community not yet processed.
            try:
                was_public, raw = reader.read_space(sp.id, max_pages=max_pages, since=since)
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
                    log_activity(s, kind="read", level="success", community=slug, space=sp.name,
                                 summary=f"{host} / {sp.name}: read {len(space_recs)} public item(s)",
                                 items_seen=len(space_recs))
            except Exception as exc:  # noqa: BLE001 - one bad space must not kill the run
                result.errors.append(f"{host}/{sp.name}: {exc.__class__.__name__}")
                with db.session() as s:
                    log_activity(s, kind="read", level="error", community=slug, space=sp.name,
                                 summary=f"{host} / {sp.name}: error {exc.__class__.__name__}, skipped")
                continue

        if not public_count:
            _mark_synced(db, slug)
            continue

        result.communities_read += 1
        result.public_spaces += public_count
        result.posts_read += len(records)

        if records:
            triage_res = triage_records(
                db, records, requirements, community=slug,
                source_url=reader.base, use_llm=use_llm, verbose_log=verbose_log,
            )
            result.leads_found += len(triage_res.leads)
        _mark_synced(db, slug)

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

    return result


def _mark_synced(db: Database, slug: str) -> None:
    with db.session() as s:
        c = s.scalar(select(Community).where(Community.slug == slug))
        if c is not None:
            c.last_synced_at = datetime.now(timezone.utc).replace(tzinfo=None)


def _lead_spaces(spaces):
    """Spaces whose name/slug suggests hiring or project requests, else all."""
    hints = ("consultant", "project", "job", "hir", "need", "opportunit",
             "collab", "client", "gig", "freelance", "request")
    lead = [s for s in spaces
            if any(h in (s.slug + " " + s.name).lower() for h in hints)]
    return lead or spaces
