"""Push newly classified Circle leads to the production Vini ingest endpoint.

Configured via env (optional — if unset, sync is a no-op):

    VINI_API_SECRET          shared secret for ``x-vini-api-secret``
    SUPABASE_ANON_KEY        project anon / publishable key (``apikey`` header)
    VINI_LEADS_INGEST_URL    override endpoint (defaults to the Warmr function)

Without ``parser: circle_scraper`` the remote treats the lead as Vini/external_ai.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

import requests
from sqlalchemy import select
from sqlalchemy.orm import Session

from circle_leads.storage.models import Author, Community, Lead, Post, utcnow

logger = logging.getLogger(__name__)

DEFAULT_INGEST_URL = (
    "https://jlhesealocmkwmphxuqb.supabase.co/functions/v1/vini_leads_ingest"
)
PARSER_NAME = "circle_scraper"
PLATFORM = "circle"
# Local classifier only files explicit hiring asks.
DEFAULT_INTENT_TYPE = "explicit"


@dataclass
class ViniIngestConfig:
    url: str
    anon_key: str
    api_secret: str

    @property
    def enabled(self) -> bool:
        return bool(self.anon_key and self.api_secret)


@dataclass
class PushResult:
    attempted: int = 0
    sent: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)


def load_vini_ingest_config() -> ViniIngestConfig:
    return ViniIngestConfig(
        url=(
            os.environ.get("VINI_LEADS_INGEST_URL") or DEFAULT_INGEST_URL
        ).strip(),
        anon_key=(
            os.environ.get("SUPABASE_ANON_KEY")
            or os.environ.get("VINI_SUPABASE_ANON_KEY")
            or ""
        ).strip(),
        api_secret=(os.environ.get("VINI_API_SECRET") or "").strip(),
    )


def _iso_utc(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def lead_to_ingest_payload(
    lead: Lead,
    post: Post,
    community: Community,
    author: Author | None,
) -> dict[str, Any] | None:
    """Map a stored lead to the Vini ingest body shape. None if unusable."""
    url = (post.url or "").strip()
    content = (post.content or "").strip()
    if not url or not content:
        return None

    community_name = (community.name or community.slug or "").strip() or "Circle"
    name = None
    if author and (author.display_name or "").strip():
        name = author.display_name.strip()
    elif (lead.company or "").strip():
        name = lead.company.strip()

    external_id = (
        f"circle:{community.slug}:{post.content_type}:{post.source_content_id}"
    )
    payload: dict[str, Any] = {
        "url": url,
        "community": community_name,
        "content": content,
        "posted_at": _iso_utc(post.published_at) or _iso_utc(lead.created_at),
        "intent_type": DEFAULT_INTENT_TYPE,
        "platform": PLATFORM,
        "parser": PARSER_NAME,
        "external_id": external_id,
    }
    if name:
        payload["name"] = name
    return payload


def post_leads_to_vini(
    items: list[dict[str, Any]],
    *,
    config: ViniIngestConfig | None = None,
    session: requests.Session | None = None,
    timeout: float = 30.0,
) -> None:
    """POST a non-empty lead array. Raises on HTTP / network failure."""
    if not items:
        return
    cfg = config or load_vini_ingest_config()
    if not cfg.enabled:
        raise RuntimeError(
            "Vini ingest is not configured "
            "(need SUPABASE_ANON_KEY and VINI_API_SECRET)."
        )

    headers = {
        "Content-Type": "application/json",
        "apikey": cfg.anon_key,
        "Authorization": f"Bearer {cfg.anon_key}",
        "x-vini-api-secret": cfg.api_secret,
    }
    http = session or requests.Session()
    response = http.post(cfg.url, json=items, headers=headers, timeout=timeout)
    if response.status_code >= 400:
        body = (response.text or "")[:500]
        raise RuntimeError(
            f"Vini ingest HTTP {response.status_code}: {body or response.reason}"
        )


def _load_lead_bundle(
    session: Session, lead_ids: Iterable[int]
) -> list[tuple[Lead, Post, Community, Author | None]]:
    ids = list(lead_ids)
    if not ids:
        return []
    stmt = (
        select(Lead, Post, Community, Author)
        .join(Post, Lead.post_id == Post.id)
        .join(Community, Post.community_id == Community.id)
        .outerjoin(Author, Post.author_id == Author.id)
        .where(Lead.id.in_(ids))
        .where(Lead.classification == "LEAD")
        .where(Lead.duplicate_of_id.is_(None))
    )
    return [
        (lead, post, community, author)
        for lead, post, community, author in session.execute(stmt).all()
    ]


def push_leads_by_ids(
    session: Session,
    lead_ids: Iterable[int],
    *,
    config: ViniIngestConfig | None = None,
    force: bool = False,
) -> PushResult:
    """Build payloads for the given lead ids and POST them; mark synced on success."""
    result = PushResult()
    ids = list(lead_ids)
    cfg = config or load_vini_ingest_config()
    if not cfg.enabled:
        result.skipped = len(ids)
        logger.debug("Vini ingest skipped: credentials not set.")
        return result

    bundles = _load_lead_bundle(session, ids)
    payloads: list[dict[str, Any]] = []
    ready_leads: list[Lead] = []

    for lead, post, community, author in bundles:
        if not force and lead.external_synced_at is not None:
            result.skipped += 1
            continue
        payload = lead_to_ingest_payload(lead, post, community, author)
        if payload is None:
            result.skipped += 1
            logger.warning(
                "Skipping lead %s for Vini ingest: missing url or content", lead.id
            )
            continue
        payloads.append(payload)
        ready_leads.append(lead)

    result.attempted = len(payloads)
    if not payloads:
        return result

    try:
        post_leads_to_vini(payloads, config=cfg)
    except Exception as exc:  # noqa: BLE001 - caller should keep local leads
        result.errors.append(str(exc))
        logger.exception("Failed to push %d lead(s) to Vini ingest", len(payloads))
        return result

    synced_at = utcnow()
    for lead in ready_leads:
        lead.external_synced_at = synced_at
    result.sent = len(ready_leads)
    return result


def push_unsynced_leads(
    session: Session,
    *,
    limit: int | None = 100,
    config: ViniIngestConfig | None = None,
) -> PushResult:
    """Push LEAD rows that have never been acknowledged by production."""
    cfg = config or load_vini_ingest_config()
    if not cfg.enabled:
        return PushResult()

    stmt = (
        select(Lead.id)
        .where(Lead.classification == "LEAD")
        .where(Lead.duplicate_of_id.is_(None))
        .where(Lead.external_synced_at.is_(None))
        .order_by(Lead.id.asc())
    )
    if limit:
        stmt = stmt.limit(limit)
    ids = list(session.scalars(stmt).all())
    return push_leads_by_ids(session, ids, config=cfg)
