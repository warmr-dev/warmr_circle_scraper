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
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

import requests
from sqlalchemy import select
from sqlalchemy.orm import Session

from circle_leads.storage.activity import log_activity
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
    {"inserted", "created", "accepted", "skipped", "ok", "duplicate", "success"}
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
# Seen from 2026-09-28: {"status": "held", "decision": "invalid_timestamp",
# "holdReason": "missing_or_invalid_fetched_at"}. The portal wants the time we
# read the post, not only the time it was written.
FETCHED_AT_HOLD = "missing_or_invalid_fetched_at"
# A hold the next push can clear, and the payload field that clears it. Sent
# without that field, the lead stays unsynced so the fixed body goes out.
# From later the same day: "missing_or_invalid_classified_at" -- when we
# judged the post a lead.
CLASSIFIED_AT_HOLD = "missing_or_invalid_classified_at"
FIXABLE_HOLDS = {AUTHOR_IDENTITY_HOLD: "source_author_id", FETCHED_AT_HOLD: "fetched_at",
                 CLASSIFIED_AT_HOLD: "classified_at"}
# One-shot: leads stamped synced before the payload carried source_author_id.
RESEND_WITH_AUTHOR_KEY = "vini_author_identity_resend"

# What Vini said about a lead, kept on the lead itself (Lead.vini_status).
# external_synced_at cannot answer "does the client see it": a parked lead is
# stamped exactly like a published one.
VINI_ACCEPTED = "accepted"
VINI_DUPLICATE = "duplicate"
VINI_HELD = "held"
VINI_REJECTED = "rejected"
VINI_ERROR = "error"  # no answer at all: HTTP error, timeout
# Landed statuses that mean "already have this one", not "took it just now".
DUPLICATE_STATUSES = frozenset({"duplicate", "skipped"})


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


def _message_token(post: Post) -> str:
    raw = (post.source_content_id or "").strip()
    return raw.split(":")[-1] or str(post.id)


def _message_id(community: Community, post: Post) -> str:
    """Identity the portal stores on the ingress receipt.

    Re-sending the same id returns the previous decision and does not insert
    a second row. A community homepage is not a message id: posts that only
    have that URL still need their own id, or they never leave the hold for
    ``missing_root_message_identity``.
    """
    return f"circle:{community.slug}:lead:{_message_token(post)}"


def _post_url(url: str, token: str) -> str:
    """One URL is one lead on the portal.

    Several posts stored with the community homepage collapse into the first
    of them (``lead_duplicate:exact``). A URL that is not already a Circle
    post permalink gets a stable path so each post is its own row.
    """
    if "/c/" in url:
        return url
    return f"{url.rstrip('/')}/c/post/{token}"


def lead_to_ingest_payload(
    lead: Lead,
    post: Post,
    community: Community,
    author: Author | None,
) -> dict[str, Any] | None:
    """Map a stored lead to the Vini ingest body shape. None if unusable."""
    # Legacy rows keep their existing contract. Every newly evaluated candidate
    # needs a successful, current decision; force/retry cannot bypass this gate.
    audit = post.classification_audit
    if audit is not None and (not post.classified or audit.get("outcome") != "lead"):
        return None
    if lead.classification != "LEAD" or lead.duplicate_of_id is not None:
        return None
    url = (post.url or "").strip()
    content = (post.content or "").strip()
    if not url or not content:
        return None

    # The portal parks a lead with no source_author_id
    # ("missing_source_author_identity", filed as decision "invalid_timestamp").
    # A Circle member id is best. Posts scraped before that id was stored still
    # have a display name; that name, scoped to the community, is enough for
    # the check. With neither, there is nothing to POST.
    name = None
    if author and (author.display_name or "").strip():
        name = author.display_name.strip()
    elif (lead.company or "").strip():
        name = lead.company.strip()
    author_id = ""
    if author and (author.source_author_id or "").strip():
        author_id = author.source_author_id.strip()
    elif name:
        author_id = f"circle:{community.slug}:{name.lower()}"
    if not author_id:
        return None

    community_name = (community.name or community.slug or "").strip() or "Circle"
    token = _message_token(post)
    external_id = _message_id(community, post)
    posted_at = _iso_utc(post.published_at) or _iso_utc(lead.created_at)
    fetched_at = _iso_utc(post.scraped_at) or _iso_utc(utcnow())
    classified_at = _iso_utc(lead.created_at) or fetched_at
    payload: dict[str, Any] = {
        "url": _post_url(url, token),
        "community": community_name,
        "content": content,
        "posted_at": posted_at,
        # The portal files a missing event time as invalid_timestamp, and
        # refuses one older than 48 hours. This is the post's own time.
        "source_event_at": posted_at,
        # When we read it. Without it the portal parks the lead
        # (missing_or_invalid_fetched_at).
        "fetched_at": fetched_at,
        # When we judged it a lead. Without it the portal parks the lead
        # (missing_or_invalid_classified_at, seen 2026-09-28). A post read
        # again after it was judged has a later scraped_at, so the verdict
        # time is never allowed to fall before the read time.
        "classified_at": max(classified_at, fetched_at),
        "delivery_mode": "live",
        "intent_type": DEFAULT_INTENT_TYPE,
        "platform": PLATFORM,
        "parser": PARSER_NAME,
        "external_id": external_id,
        "source_author_id": author_id,
        "root_message_id": external_id,
        "source_message_id": external_id,
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


def _record_answer(lead: Lead, status: str, reason: str | None, ref: Any,
                   at: datetime) -> bool:
    """Keep what Vini said about ``lead``. True when it differs from before."""
    reason = (reason or "")[:300] or None
    changed = (lead.vini_status, lead.vini_reason) != (status, reason)
    lead.vini_status = status
    lead.vini_reason = reason
    if ref not in (None, ""):
        lead.vini_ref = str(ref)[:64]
    lead.vini_attempts = (lead.vini_attempts or 0) + 1
    lead.vini_last_attempt_at = at
    lead.vini_responded_at = at
    return changed


def _record_no_answer(lead: Lead, error: str, at: datetime) -> bool:
    """A push that got no answer at all: an HTTP error, a timeout.

    That says nothing about what Vini thinks of the lead, so an earlier answer
    stays as it was. Only a lead Vini has never answered is marked ``error``.
    """
    lead.vini_attempts = (lead.vini_attempts or 0) + 1
    lead.vini_last_attempt_at = at
    if lead.vini_status not in (None, VINI_ERROR):
        return False
    reason = (error or "")[:300] or None
    changed = (lead.vini_status, lead.vini_reason) != (VINI_ERROR, reason)
    lead.vini_status = VINI_ERROR
    lead.vini_reason = reason
    return changed


def _log_answers(session: Session, changed: list[Lead], slug_of: dict[int, str],
                 *, error: str | None = None) -> None:
    """One activity row when Vini's answer about some lead changed.

    Only on a change: the watcher drains after every batch and a refused lead
    is sent again each time, so a row per push would bury the log in copies.
    Ids go in one key per status because the activity log cuts any single
    value at 2000 characters.
    """
    if not changed:
        return
    by_status: dict[str, list[int]] = {}
    reasons: dict[str, int] = {}
    for lead in changed:
        by_status.setdefault(lead.vini_status or "", []).append(lead.id)
        if lead.vini_status in (VINI_HELD, VINI_REJECTED, VINI_ERROR) and lead.vini_reason:
            key = lead.vini_reason[:80]
            reasons[key] = reasons.get(key, 0) + 1
    detail: dict[str, Any] = {
        f"{status}_ids": ",".join(str(i) for i in ids[:100])
        for status, ids in by_status.items()
    }
    detail["counts"] = {status: len(ids) for status, ids in by_status.items()}
    if reasons:
        top = sorted(reasons.items(), key=lambda kv: -kv[1])[:15]
        detail["reasons"] = dict(top)
    if error:
        detail["error"] = error[:300]
    slugs = {slug_of.get(lead.id) for lead in changed}
    if VINI_ERROR in by_status:
        level = "error"
    elif VINI_HELD in by_status or VINI_REJECTED in by_status:
        level = "warning"
    else:
        level = "success"
    log_activity(
        session,
        kind="export",
        level=level,
        community=slugs.pop() if len(slugs) == 1 else None,
        summary="Vini answered: " + ", ".join(
            f"{status} {len(ids)}" for status, ids in sorted(by_status.items())
        ),
        detail=detail,
        leads_found=len(by_status.get(VINI_ACCEPTED, [])),
    )


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
    slug_of: dict[int, str] = {}

    for lead, post, community, author in bundles:
        if not force and lead.external_synced_at is not None:
            result.skipped += 1
            continue
        payload = lead_to_ingest_payload(lead, post, community, author)
        if payload is None:
            result.skipped += 1
            logger.warning(
                "Skipping lead %s for Vini ingest: missing url, content, "
                "or an author name",
                lead.id,
            )
            continue
        payloads.append(payload)
        ready_leads.append(lead)
        slug_of[lead.id] = community.slug

    result.attempted = len(payloads)
    if not payloads:
        return result

    try:
        results = post_leads_to_vini(payloads, config=cfg)
    except Exception as exc:  # noqa: BLE001 - caller should keep local leads
        result.errors.append(str(exc))
        logger.exception("Failed to push %d lead(s) to Vini ingest", len(payloads))
        at = utcnow()
        changed = [lead for lead in ready_leads if _record_no_answer(lead, str(exc), at)]
        _log_answers(session, changed, slug_of, error=str(exc))
        return result

    synced_at = utcnow()
    changed: list[Lead] = []
    for index, lead in enumerate(ready_leads):
        item = results[index] if index < len(results) else None
        if item is None:
            # No per-item answer (older endpoint, or a shorter list than we
            # sent). Keep the old behaviour: a 2xx counts as delivered.
            lead.external_synced_at = synced_at
            result.sent += 1
            if _record_answer(lead, VINI_ACCEPTED, "no_item_result", None, synced_at):
                changed.append(lead)
            continue

        status = str(item.get("status") or "").strip().lower()
        ref = item.get("id") or item.get("lead_id") or item.get("leadId")
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
            if _record_answer(lead, VINI_HELD, reason, ref, synced_at):
                changed.append(lead)
            sent = payloads[index] or {}
            if any(hold in reason and not sent.get(field_name)
                   for hold, field_name in FIXABLE_HOLDS.items()):
                # The body can still be fixed. Leave it unsynced so the next
                # push sends the missing field instead of parking it again.
                continue
            # Any other hold, or one of these after we already sent the
            # field: a retry of the same body will not clear it.
            lead.external_synced_at = synced_at
            result.sent += 1
            continue
        if status in LANDED_STATUSES:
            lead.external_synced_at = synced_at
            result.sent += 1
            answer = VINI_DUPLICATE if status in DUPLICATE_STATUSES else VINI_ACCEPTED
            if _record_answer(lead, answer, status, ref, synced_at):
                changed.append(lead)
        else:
            # Leave external_synced_at NULL so push_unsynced_leads picks it up
            # again. Stamping it was how leads that never arrived came to be
            # recorded as delivered.
            message = str(item.get("error") or item.get("reason") or "")[:300]
            result.rejected.append((lead.id, status or "(no status)", message))
            reason = f"{status or '(no status)'}: {message}" if message else (status or "(no status)")
            if _record_answer(lead, VINI_REJECTED, reason, ref, synced_at):
                changed.append(lead)

    _log_answers(session, changed, slug_of)

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
        .order_by(Lead.id.asc())
    )
    if limit:
        stmt = stmt.limit(limit)
    ids = list(session.scalars(stmt).all())
    return push_leads_by_ids(session, ids, config=cfg)


def retry_failed_leads(session: Session, *, limit: int = 25,
                       config: ViniIngestConfig | None = None,
                       now: datetime | None = None) -> PushResult:
    """Retry only recent, previously attempted transport failures, never history.

    The existing stable ingress identity reconciles an ambiguous timeout. Normal
    push gates still apply; no force, timestamp rewrite or held/rejected replay.
    """
    cfg = config or load_vini_ingest_config()
    if not cfg.enabled:
        return PushResult()
    now = (now or utcnow()).replace(tzinfo=None)
    # Timestamp arithmetic differs between SQLite and PostgreSQL. Four
    # bounded selections express the durable backoff without dialect-specific SQL.
    ids = []
    for attempt_filter, minutes in ((Lead.vini_attempts <= 1, 1),
                                    (Lead.vini_attempts == 2, 5),
                                    (Lead.vini_attempts == 3, 15),
                                    (Lead.vini_attempts >= 4, 60)):
        rows = session.execute(select(Lead.id, Lead.vini_last_attempt_at).join(Post).where(
            Lead.classification == "LEAD", Lead.duplicate_of_id.is_(None),
            Lead.external_synced_at.is_(None), Lead.vini_status == VINI_ERROR,
            Lead.vini_attempts > 0, attempt_filter,
            Lead.vini_last_attempt_at >= now - timedelta(hours=48),
            Lead.vini_last_attempt_at <= now - timedelta(minutes=minutes),
            Post.published_at >= now - timedelta(hours=48),
            Post.published_at <= now, Post.classified.is_(True),
        ).order_by(Lead.vini_last_attempt_at, Lead.id).limit(max(1, min(limit, 25)))).all()
        ids.extend(rows)
    ids.sort(key=lambda row: (row[1], row[0]))
    return push_leads_by_ids(session, [row[0] for row in ids[:max(1, min(limit, 25))]], config=cfg)
