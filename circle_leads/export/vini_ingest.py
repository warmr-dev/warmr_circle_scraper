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

from circle_leads.storage.models import Author, Community, Lead, Post, Setting, utcnow

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
    # What the endpoint said about each item, e.g. {"inserted": 3, "error": 1}.
    # Empty when the response carried no per-item breakdown.
    outcomes: dict[str, int] = field(default_factory=dict)
    # (lead id, status, message) for every item the endpoint did not take.
    rejected: list[tuple[int, str, str]] = field(default_factory=list)
    # (lead id, reason) for items the endpoint parked instead of publishing.
    held: list[tuple[int, str]] = field(default_factory=list)


# Per-item statuses that mean the lead is on the far side and needs no retry.
# "skipped"/"duplicate" are the endpoint's words for "already have this one",
# which is a success from here -- re-sending forever would not help.
LANDED_STATUSES = frozenset(
    {"inserted", "accepted", "skipped", "ok", "duplicate", "success"}
)

# "held" is its own thing: the endpoint took the lead but parked it, so the
# client does not see it. A hold whose cause a retry cannot change is marked
# as sent AND reported, so we do not POST the same body forever.
# "missing_source_author_identity" is the exception: the portal files it under
# decision "invalid_timestamp", and it clears once the body carries
# source_author_id. Stamping that one is how a lead stayed parked after the
# id was already sitting on the author row.
# Observed holds: {"status": "held", "decision": "invalid_timestamp",
# "holdReason": "missing_source_author_identity"}.
HELD_STATUS = "held"
AUTHOR_IDENTITY_HOLD = "missing_source_author_identity"
# One-shot: leads stamped synced before the payload carried source_author_id.
RESEND_WITH_AUTHOR_KEY = "vini_author_identity_resend"


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

    # The portal parks a lead that has a display name and no stable author id
    # ("missing_source_author_identity", filed as decision "invalid_timestamp").
    # A name alone is not an identity: the parsers that land always send
    # source_author_id. Without one there is nothing to POST.
    author_id = ""
    if author and (author.source_author_id or "").strip():
        author_id = author.source_author_id.strip()
    if not author_id:
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
        "source_author_id": author_id,
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
) -> list[dict[str, Any]]:
    """POST a non-empty lead array. Raises on HTTP / network failure.

    Returns the endpoint's per-item results, in the order the items were sent,
    or an empty list if the response carried none.

    A 2xx does NOT mean the leads arrived. The endpoint answers 200 with a
    body like::

        {"ok": true, "received": 1, "inserted": 0, "accepted": 0, "held": 0,
         "discarded": 0, "historical_expired": 0,
         "results": [{"status": "error", "error": "content is required"}]}

    This function used to return None and the caller stamped every lead as
    synced on any 2xx, so a rejected lead was recorded as delivered and never
    retried.
    """
    if not items:
        return []
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

    try:
        body = response.json()
    except ValueError:
        # A 2xx with an unreadable body: treat it as no per-item information
        # rather than as a failure, so behaviour matches the older endpoint.
        logger.warning("Vini ingest returned %s with a non-JSON body",
                       response.status_code)
        return []
    if not isinstance(body, dict):
        return []
    results = body.get("results")
    if not isinstance(results, list):
        return []
    return [r if isinstance(r, dict) else {"status": str(r)} for r in results]


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
                "Skipping lead %s for Vini ingest: missing url, content, "
                "or source_author_id",
                lead.id,
            )
            continue
        payloads.append(payload)
        ready_leads.append(lead)

    result.attempted = len(payloads)
    if not payloads:
        return result

    try:
        results = post_leads_to_vini(payloads, config=cfg)
    except Exception as exc:  # noqa: BLE001 - caller should keep local leads
        result.errors.append(str(exc))
        logger.exception("Failed to push %d lead(s) to Vini ingest", len(payloads))
        return result

    synced_at = utcnow()
    for index, lead in enumerate(ready_leads):
        item = results[index] if index < len(results) else None
        if item is None:
            # No per-item answer (older endpoint, or a shorter list than we
            # sent). Keep the old behaviour: a 2xx counts as delivered.
            lead.external_synced_at = synced_at
            result.sent += 1
            continue

        status = str(item.get("status") or "").strip().lower()
        result.outcomes[status or "(no status)"] = (
            result.outcomes.get(status or "(no status)", 0) + 1
        )
        if status == HELD_STATUS:
            # Parked, not delivered. Record why so a human can act.
            reason = " ".join(
                str(item.get(k)) for k in ("decision", "holdReason", "hold_reason")
                if item.get(k)
            )
            result.held.append((lead.id, reason[:300]))
            sent_identity = bool((payloads[index] or {}).get("source_author_id"))
            if AUTHOR_IDENTITY_HOLD in reason and not sent_identity:
                # The body can still be fixed. Leave it unsynced so the next
                # push sends source_author_id instead of parking it again.
                continue
            # Any other hold, or this one after we already sent the id: a
            # retry of the same body will not clear it.
            lead.external_synced_at = synced_at
            result.sent += 1
            continue
        if status in LANDED_STATUSES:
            lead.external_synced_at = synced_at
            result.sent += 1
        else:
            # Leave external_synced_at NULL so push_unsynced_leads picks it up
            # again. Stamping it was how leads that never arrived came to be
            # recorded as delivered.
            message = str(item.get("error") or item.get("reason") or "")[:300]
            result.rejected.append((lead.id, status or "(no status)", message))

    if result.held:
        logger.error(
            "Vini parked %d of %d lead(s), the client will not see them: %s",
            len(result.held), len(ready_leads),
            "; ".join(reason for _, reason in result.held[:3]),
        )
        _alert_held(result)
    if result.rejected:
        logger.error(
            "Vini ingest refused %d of %d lead(s): %s",
            len(result.rejected), len(ready_leads), result.outcomes,
        )
        _alert_rejected(result)
    return result


def _alert_held(result: PushResult) -> None:
    """A held lead looks delivered on every dashboard and is invisible to the
    client. That combination is exactly how two weeks went by unnoticed."""
    try:
        from circle_leads.notify import notify

        reasons: dict[str, int] = {}
        for _, reason in result.held:
            reasons[reason or "(без причины)"] = reasons.get(reason or "(без причины)", 0) + 1
        notify(
            f"Vini придержал {len(result.held)} лид(ов) — заказчик их не видит",
            "\n".join(f"{reason}: {count}" for reason, count in sorted(reasons.items())),
            level="error",
            dedup_key="vini-held",
        )
    except Exception:  # noqa: BLE001 - an alert must never break the push
        logger.warning("could not send the Vini hold alert", exc_info=True)


def _alert_rejected(result: PushResult) -> None:
    """Tell a human. A lead the client never sees is the one failure that
    makes the whole pipeline pointless, and it was silent for two weeks."""
    try:
        from circle_leads.notify import notify

        lines = [f"{status}: {count}" for status, count in sorted(result.outcomes.items())]
        sample = "\n".join(
            f"#{lead_id} {status} {message}"[:200]
            for lead_id, status, message in result.rejected[:5]
        )
        notify(
            f"Vini не принял {len(result.rejected)} лид(ов)",
            "\n".join(lines) + ("\n\n" + sample if sample else ""),
            level="error",
            dedup_key="vini-rejected",
        )
    except Exception:  # noqa: BLE001 - an alert must never break the push
        logger.warning("could not send the Vini rejection alert", exc_info=True)


def release_leads_sent_without_author_identity(session: Session) -> int:
    """Unstamp leads the portal parked for a missing author id.

    Runs once. A lead already in ``leads`` comes back as a duplicate, which
    counts as delivered. A lead that was only parked is sent again, this time
    with ``source_author_id``. Leads whose author still has no id stay put:
    there is nothing new to send until a later read fills the id in.
    """
    if session.get(Setting, RESEND_WITH_AUTHOR_KEY) is not None:
        return 0
    leads = list(session.scalars(
        select(Lead)
        .join(Post, Lead.post_id == Post.id)
        .join(Author, Post.author_id == Author.id)
        .where(Lead.classification == "LEAD")
        .where(Lead.duplicate_of_id.is_(None))
        .where(Lead.external_synced_at.is_not(None))
        .where(Author.source_author_id.is_not(None))
        .where(Author.source_author_id != "")
    ).all())
    for lead in leads:
        lead.external_synced_at = None
    session.add(Setting(key=RESEND_WITH_AUTHOR_KEY, value="1"))
    session.flush()
    if leads:
        logger.info(
            "Vini: %d lead(s) queued again now that the author id is sent",
            len(leads),
        )
    return len(leads)


def drain_unsynced_leads(db, *, limit: int = 25) -> PushResult:
    """Release the author-identity backlog once, then push unsynced leads.

    No-op when ingest is not configured. Callers (the watcher, a harvest)
    use this so a lead parked earlier still goes out without a manual
    ``push-leads``.
    """
    cfg = load_vini_ingest_config()
    if not cfg.enabled:
        return PushResult()
    with db.session() as s:
        release_leads_sent_without_author_identity(s)
        return push_unsynced_leads(s, limit=limit, config=cfg)


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
        .join(Post, Lead.post_id == Post.id)
        .join(Author, Post.author_id == Author.id)
        .where(Lead.classification == "LEAD")
        .where(Lead.duplicate_of_id.is_(None))
        .where(Lead.external_synced_at.is_(None))
        # A display name with no Circle member id is what the portal parks.
        # Leave those rows unsynced, but do not let them fill the batch: the
        # next read that learns the id is what makes them sendable.
        .where(Author.source_author_id.is_not(None))
        .where(Author.source_author_id != "")
        .order_by(Lead.id.asc())
    )
    if limit:
        stmt = stmt.limit(limit)
    ids = list(session.scalars(stmt).all())
    return push_leads_by_ids(session, ids, config=cfg)
