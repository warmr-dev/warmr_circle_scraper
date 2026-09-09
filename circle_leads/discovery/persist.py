"""Persist ranked community finds and flag which are new.

A search is only useful automatically if a re-run tells you what changed. This
stores each find in the ``communities`` table and reports which slugs were seen
for the first time on this run, so a scheduled search surfaces fresh candidates
rather than the same list every day.

Storing a find records *that the community exists and how it scored* -- nothing
about joining it. `permission_status` stays `candidate`; a find never implies
access or approval.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sqlalchemy import select

from circle_leads.scraper.http_client import shared_session

from circle_leads.discovery.discover_communities import detect_platform
from circle_leads.discovery.finder import RankedCommunity
from circle_leads.discovery.validate_finds import (
    is_subdomain_community,
    is_interstitial_title,
    validate_url,
)
from circle_leads.storage.activity import log_activity
from circle_leads.storage.database import Database
from circle_leads.storage.models import AccessState, Community, PermissionStatus

logger = logging.getLogger(__name__)


@dataclass
class PersistResult:
    new: list[RankedCommunity] = field(default_factory=list)
    updated: list[RankedCommunity] = field(default_factory=list)
    unchanged: int = 0

    @property
    def new_count(self) -> int:
        return len(self.new)


def _looks_like_circle_subdomain(rc: RankedCommunity) -> bool:
    return ".circle.so" in (rc.join_url or "")


def persist_finds(
    db: Database,
    ranked: list[RankedCommunity],
    *,
    niche: str | None = None,
    min_score: int = 0,
    source: str = "search",
    validate: bool = True,
    session=None,
) -> PersistResult:
    """Store finds; return which slugs are new since the last run.

    A find already in the DB updates its score and reasons (a re-search may see
    it differently) but is never re-flagged as new.
    """
    result = PersistResult()

    # Validate URLs (drop soft-404 Discover pages, confirm subdomains are live)
    # and mark the real <slug>.circle.so communities, which are the joinable
    # ones. Discover /products pages are kept only if they load a real page.
    http = session or shared_session()
    validated: list[RankedCommunity] = []
    for rc in ranked:
        if rc.score < min_score:
            continue
        if validate:
            v = validate_url(rc.join_url, session=http)
            if not v.ok:
                logger.info("Dropping dead find %s (%s)", rc.slug, v.reason)
                continue
            if v.is_free is not None:
                rc.is_free = v.is_free
            if getattr(v, "price", None):
                rc.price_label = v.price
            elif v.is_free:
                rc.price_label = "Free"
            # The user wants free communities, so push confirmed-paid ones down.
            if v.is_free is False and "paid" not in rc.reasons:
                rc.score = max(0, rc.score - 25)
                rc.reasons = list(rc.reasons) + ["paid"]
            if v.title and (not rc.name or rc.name == rc.slug):
                rc.name = v.title.split("|")[0].strip()[:80] or rc.name
        # A real community subdomain is worth more than a Discover listing.
        # Idempotent: only boost once, tracked by the reason tag, so a re-run
        # does not keep inflating the score.
        if is_subdomain_community(rc.join_url) and "community_subdomain" not in rc.reasons:
            rc.score = min(100, rc.score + 15)
            rc.reasons = list(rc.reasons) + ["community_subdomain"]
        validated.append(rc)

    with db.session() as s:
        for rc in validated:
            platform = detect_platform(rc.join_url, session=http)
            existing = s.scalar(select(Community).where(Community.slug == rc.slug))
            if existing is None:
                community = Community(
                    slug=rc.slug,
                    name=rc.name,
                    url=rc.join_url,
                    platform=platform,
                    discovery_source=f"{source}:{niche}" if niche else source,
                    access_status=AccessState.NOT_VISITED.value,
                    permission_status=PermissionStatus.CANDIDATE.value,
                    relevance_score=rc.score,
                    relevance_reasons=rc.reasons,
                    relevant=rc.score >= 30,
                    price_label=rc.price_label,
                    description=rc.description,
                )
                s.add(community)
                result.new.append(rc)
            else:
                changed = False
                if platform and existing.platform != platform:
                    existing.platform = platform
                    changed = True
                if rc.score > (existing.relevance_score or 0):
                    existing.relevance_score = rc.score
                    existing.relevance_reasons = rc.reasons
                    existing.relevant = rc.score >= 30
                    changed = True
                if rc.name and not existing.name:
                    existing.name = rc.name
                    changed = True
                # Repair a name that a bot-check page corrupted on an earlier
                # run (e.g. "Verifying you are a human" from a datacenter IP).
                elif existing.name and is_interstitial_title(existing.name):
                    existing.name = rc.name or rc.slug
                    changed = True
                if rc.price_label and not existing.price_label:
                    existing.price_label = rc.price_label
                    changed = True
                if changed:
                    result.updated.append(rc)
                else:
                    result.unchanged += 1

        log_activity(
            s,
            kind="discover",
            level="success" if result.new else "info",
            summary=(
                f"Search '{niche}': {result.new_count} new, "
                f"{len(result.updated)} updated, {result.unchanged} unchanged"
            ),
            detail={
                "niche": niche,
                "source": source,
                "new_slugs": ", ".join(c.slug for c in result.new[:20]),
            },
            items_seen=len(ranked),
            leads_found=result.new_count,
        )

    return result


def new_since(db: Database, *, limit: int = 50) -> list[dict]:
    """Communities discovered but not yet visited, best score first."""
    with db.session() as s:
        rows = s.scalars(
            select(Community)
            .where(Community.access_status == AccessState.NOT_VISITED.value)
            .order_by(Community.relevance_score.desc())
            .limit(limit)
        ).all()
        return [
            {
                "slug": c.slug,
                "name": c.name,
                "join_url": c.url,
                "score": c.relevance_score,
                "reasons": c.relevance_reasons or [],
                "price_label": c.price_label,
                "discovered_at": c.discovered_at.isoformat() if c.discovered_at else None,
                "source": c.discovery_source,
            }
            for c in rows
        ]
