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
from datetime import datetime, timezone

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


def _community_hosts(db: Database, *, only_new: bool, limit: int) -> list[tuple[str, str]]:
    """Return (host, slug) for community subdomains worth reading.

    When ``only_new`` is set, restrict to communities not yet read (no
    ``last_synced_at``), so a scheduled run focuses on fresh finds.
    """
    with db.session() as s:
        stmt = select(Community).order_by(Community.relevance_score.desc())
        rows = list(s.scalars(stmt).all())
        out = []
        for c in rows:
            if not is_subdomain_community(c.url):
                continue  # Discover listings can't be read; only subdomains
            if only_new and c.last_synced_at is not None:
                continue
            host = c.url.replace("https://", "").replace("http://", "").strip("/")
            out.append((host, c.slug))
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
) -> HarvestResult:
    """Discover public communities and read their public spaces for leads."""
    niches = niches or DEFAULT_NICHES
    result = HarvestResult(niches=niches)

    # --- 1. Discover new communities via search --------------------------
    if search:
        for niche in niches:
            try:
                disc = discover_by_search(niche)
                res = persist_finds(
                    db, disc.ranked, niche=niche, min_score=min_score,
                    source="harvest",
                )
                result.new_communities += res.new_count
            except Exception as exc:  # noqa: BLE001
                result.errors.append(f"search '{niche}': {exc.__class__.__name__}")

    # --- 2. Read every readable community's public spaces ----------------
    hosts = _community_hosts(db, only_new=only_new, limit=max_communities)
    for host, slug in hosts:
        try:
            reader = PublicReader(host)
            spaces, records = discover_and_read_public(
                reader,
                only_lead_spaces=True,
                excluded_content=requirements.excluded_content,
                max_pages=max_pages,
            )
        except Exception as exc:  # noqa: BLE001
            result.errors.append(f"read '{host}': {exc.__class__.__name__}")
            continue

        public = [sp for sp in spaces if sp.is_public]
        if not public:
            _mark_synced(db, slug)  # checked, nothing public -- don't recheck constantly
            continue

        result.communities_read += 1
        result.public_spaces += len(public)
        result.posts_read += len(records)

        if records:
            triage_res = triage_records(
                db, records, requirements, community=slug,
                source_url=reader.base, use_llm=use_llm,
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
