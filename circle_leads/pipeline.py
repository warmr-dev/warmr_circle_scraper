"""End-to-end pipeline: discover -> validate -> ingest -> classify -> score.

Ingestion is gated on operator approval at three points: the permission file's
status, the per-space allowlist, and the DM exclusion in the scraper. A
community missing any of these yields zero collected items rather than an error.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable

import requests
from sqlalchemy import select

from circle_leads.authentication.browser_session import (
    CIRCLE_BASE,
    AdminCredentials,
    MemberSession,
    MissingCredentialError,
    NotAuthorizedError,
    mint_member_session,
    resolve_admin_credentials,
)
from circle_leads.classifier.ai_classifier import AnthropicBackend, LlmBackend
from circle_leads.classifier.lead_classifier import classify, meets_requirements
from circle_leads.config.settings import CommunityPermission, Requirements
from circle_leads.discovery.discover_communities import DiscoveredCommunity
from circle_leads.discovery.validate_community import assess_relevance, check_public_access
from circle_leads.scoring.lead_scoring import score_lead
from circle_leads.scraper import chat_scraper, comments_scraper, community_scraper, posts_scraper
from circle_leads.scraper.pagination import AccessDeniedError, ApiError, CircleClient, QuotaTracker
from circle_leads.storage.database import (
    Database,
    find_near_duplicate,
    get_or_create_author,
    get_or_create_community,
    get_or_create_space,
    upsert_post,
)
from circle_leads.storage.models import (
    AccessState,
    Community,
    Lead,
    Post,
    RunState,
    ScrapeRun,
    utcnow,
)

logger = logging.getLogger(__name__)


@dataclass
class RunSummary:
    community: str
    state: str = RunState.DISCOVERING.value
    items_seen: int = 0
    items_new: int = 0
    items_updated: int = 0
    leads_found: int = 0
    duplicates: int = 0
    errors: list[str] = field(default_factory=list)


def discover(
    db: Database, communities: Iterable[DiscoveredCommunity], *, validate: bool = True
) -> list[str]:
    """Record discovered communities and assess public relevance."""
    session_http = requests.Session()
    recorded: list[str] = []

    with db.session() as s:
        for dc in communities:
            community = get_or_create_community(
                s,
                slug=dc.slug,
                url=dc.url,
                name=dc.name,
                description=dc.description,
                price_label=dc.price_label,
                discovery_source=dc.source,
            )
            if validate:
                check = check_public_access(dc.url, session=session_http)
                community.access_status = check.access_status
                community.name = community.name or check.name
                community.description = community.description or check.description
                if check.requires_login:
                    community.notes = check.note

            assessment = assess_relevance(
                community.name, community.description, dc.metadata.get("extra")
            )
            community.relevance_score = assessment.score
            community.relevance_reasons = assessment.reasons
            community.relevant = assessment.relevant
            recorded.append(dc.slug)

    return recorded


def _build_client(
    perm: CommunityPermission, requirements: Requirements, quota: QuotaTracker
) -> tuple[CircleClient, str]:
    """Construct an authorized client for the community's approved route."""
    rl = requirements.rate_limit

    if perm.ingestion_route == "headless_member":
        member: MemberSession = mint_member_session(perm)
        holder = {"session": member}

        def headers() -> dict[str, str]:
            current = holder["session"]
            if current.is_expired:
                holder["session"] = mint_member_session(perm)
            return holder["session"].headers()

        return (
            CircleClient(
                CIRCLE_BASE,
                headers,
                requests_per_minute=rl.requests_per_minute,
                max_retries=rl.max_retries,
                backoff_base=rl.backoff_base_seconds,
                max_backoff=rl.max_backoff_seconds,
                quota=quota,
            ),
            "headless_member",
        )

    creds: AdminCredentials = resolve_admin_credentials(perm)
    return (
        CircleClient(
            CIRCLE_BASE,
            creds.headers,
            requests_per_minute=rl.requests_per_minute,
            max_retries=rl.max_retries,
            backoff_base=rl.backoff_base_seconds,
            max_backoff=rl.max_backoff_seconds,
            quota=quota,
        ),
        "admin_api_v2",
    )


def ingest_community(
    db: Database,
    perm: CommunityPermission,
    requirements: Requirements,
    *,
    incremental: bool = True,
    include_comments: bool = True,
    max_pages: int | None = None,
    request_budget: int | None = None,
) -> RunSummary:
    """Collect approved content from one community."""
    summary = RunSummary(community=perm.community_id)

    if not perm.is_approved:
        summary.state = RunState.REQUIRES_MANUAL_ACTION.value
        summary.errors.append(
            f"permission_status is '{perm.permission_status}', not 'approved'. "
            "Obtain operator approval before ingesting."
        )
        return summary

    quota = QuotaTracker(budget=request_budget)
    try:
        client, route = _build_client(perm, requirements, quota)
    except (NotAuthorizedError, MissingCredentialError) as exc:
        summary.state = RunState.FAILED.value
        summary.errors.append(str(exc))
        return summary

    with db.session() as s:
        community = get_or_create_community(
            s,
            slug=perm.community_id,
            url=perm.community_url or f"https://{perm.community_id}.circle.so",
        )
        community.permission_status = perm.permission_status
        community.access_status = AccessState.JOINED.value
        community.operator_contact = perm.operator_contact
        community.approval_reference = perm.approval_reference
        community.ingestion_route = route
        community_pk = community.id
        community_url = community.url
        watermark = community.last_synced_at if incremental else None

        run = ScrapeRun(
            community_id=community_pk,
            state=RunState.SCRAPING.value,
            route=route,
            cursor_created_at_gt=watermark,
        )
        s.add(run)
        s.flush()
        run_pk = run.id

    # Union, not precedence: a permission file listing fewer exclusions must
    # never widen what requirements.yaml forbids, and vice versa.
    excluded = sorted(
        set(perm.excluded_content or []) | set(requirements.excluded_content or [])
    )

    try:
        _collect(
            db, client, perm, route, community_pk, community_url, watermark,
            excluded, summary, include_comments, max_pages,
        )
        summary.state = RunState.COMPLETE.value
    except AccessDeniedError as exc:
        # A 401/403 is a stop condition, not a retry condition.
        summary.state = RunState.FAILED.value
        summary.errors.append(
            f"Access denied ({exc.status}). Approval may have been revoked. Stopping."
        )
    except ApiError as exc:
        summary.state = RunState.FAILED.value
        summary.errors.append(str(exc))
    except NotAuthorizedError as exc:
        # Approval can be withdrawn mid-run: the member JWT is re-minted when
        # it expires, and that re-mint fails once the operator revokes.
        summary.state = RunState.FAILED.value
        summary.errors.append(f"Authorization withdrawn during run: {exc}")
    except Exception as exc:  # noqa: BLE001 - the run row must never be orphaned
        summary.state = RunState.FAILED.value
        summary.errors.append(f"{exc.__class__.__name__}: {exc}")
        logger.exception("Unexpected failure ingesting '%s'", perm.community_id)
    finally:
        with db.session() as s:
            run = s.get(ScrapeRun, run_pk)
            if run:
                run.state = summary.state
                run.finished_at = utcnow()
                run.items_seen = summary.items_seen
                run.items_new = summary.items_new
                run.items_updated = summary.items_updated
                run.error = "; ".join(summary.errors) or None
            if summary.state == RunState.COMPLETE.value:
                community = s.get(Community, community_pk)
                if community:
                    community.last_synced_at = utcnow()

    return summary


def _collect(
    db, client, perm, route, community_pk, community_url, watermark,
    excluded, summary, include_comments, max_pages,
):
    """Fetch approved spaces and rooms, storing normalized records."""
    if route == "headless_member":
        spaces = community_scraper.list_spaces_member(client)
    else:
        spaces = community_scraper.list_spaces_admin(client)

    approved = community_scraper.approved_spaces(spaces, perm)
    if not approved:
        summary.errors.append(
            "No approved spaces matched. Populate allowed_space_ids after the "
            "operator names the spaces they approved."
        )

    fetch_posts = (
        posts_scraper.fetch_posts_member
        if route == "headless_member"
        else posts_scraper.fetch_posts_admin
    )
    fetch_comments = (
        comments_scraper.fetch_comments_member
        if route == "headless_member"
        else comments_scraper.fetch_comments_admin
    )

    for space in approved:
        space_source_id = str(space.get("id"))
        with db.session() as s:
            sp = get_or_create_space(
                s,
                community_id=community_pk,
                source_space_id=space_source_id,
                name=space.get("name"),
                slug=space.get("slug"),
                space_type=space.get("space_type") or space.get("type"),
                url=space.get("url"),
                approved=True,
            )
            space_pk = sp.id

        post_ids: list[str] = []
        for record in fetch_posts(
            client,
            space_source_id,
            since=watermark,
            max_pages=max_pages,
            community_url=community_url,
            permission_reference=perm.approval_reference,
            excluded_content=excluded,
        ):
            _store(db, community_pk, space_pk, record, summary)
            post_ids.append(record["source_content_id"])

        if include_comments:
            for post_id in post_ids:
                for record in fetch_comments(
                    client,
                    post_id,
                    max_pages=max_pages,
                    community_url=community_url,
                    permission_reference=perm.approval_reference,
                    excluded_content=excluded,
                ):
                    _store(db, community_pk, space_pk, record, summary)

    # Chat rooms are member-API only, and DMs are excluded at enumeration.
    if route == "headless_member" and perm.allowed_chat_room_uuids:
        rooms = community_scraper.list_group_chat_rooms(client)
        for room in community_scraper.approved_chat_rooms(rooms, perm):
            for record in chat_scraper.fetch_chat_messages(
                client,
                room,
                since=watermark,
                community_url=community_url,
                permission_reference=perm.approval_reference,
                excluded_content=excluded,
            ):
                _store(db, community_pk, None, record, summary)


def _store(db, community_pk, space_pk, record, summary) -> None:
    if not (record.get("content") or "").strip():
        return
    with db.session() as s:
        author_info = record.get("author") or {}
        author = get_or_create_author(
            s,
            community_id=community_pk,
            source_author_id=author_info.get("source_author_id"),
            display_name=author_info.get("display_name"),
            profile_url=author_info.get("profile_url"),
        )
        payload = dict(record)
        payload["space_id"] = space_pk
        payload["author_id"] = author.id if author else None
        _, outcome = upsert_post(s, community_id=community_pk, record=payload)

    summary.items_seen += 1
    if outcome == "new":
        summary.items_new += 1
    elif outcome == "updated":
        summary.items_updated += 1


def classify_pending(
    db: Database,
    requirements: Requirements,
    *,
    use_llm: bool = False,
    limit: int | None = None,
) -> dict[str, int]:
    """Classify every unclassified post and score the leads."""
    llm: LlmBackend | None = None
    model_name = None
    if use_llm:
        try:
            backend = AnthropicBackend()
            llm, model_name = backend, backend.model
        except RuntimeError as exc:
            logger.warning("Semantic classification unavailable: %s", exc)

    stats = {"classified": 0, "leads": 0, "not_leads": 0, "duplicates": 0, "filtered": 0}

    with db.session() as s:
        # Select ids only: the ORM objects would be detached once this session
        # closes, and each post is re-loaded in its own transaction below.
        query = select(Post.id).where(Post.classified.is_(False)).order_by(Post.id)
        if limit:
            query = query.limit(limit)
        pending_ids = list(s.scalars(query).all())

    for post_pk in pending_ids:
        with db.session() as s:
            post = s.get(Post, post_pk)
            if post is None:
                continue

            result = classify(
                post.content, requirements, llm=llm, model_name=model_name
            )
            post.classified = True
            stats["classified"] += 1

            if not result.is_lead:
                stats["not_leads"] += 1
                existing = s.scalar(select(Lead).where(Lead.post_id == post.id))
                if existing:
                    s.delete(existing)
                continue

            # The confidence floor and role/skill filters always apply.
            # `exclude_job_seekers` governs job-seeker handling, not whether
            # requirements are enforced at all.
            if not meets_requirements(result, requirements):
                stats["filtered"] += 1
                # Drop any prior lead: after an edit the stored score, evidence
                # quote, and extracted fields describe text that is now gone.
                stale = s.scalar(select(Lead).where(Lead.post_id == post.id))
                if stale:
                    s.delete(stale)
                continue

            score, priority, breakdown = score_lead(
                result, requirements, published_at=post.published_at
            )

            duplicate = find_near_duplicate(s, post)
            duplicate_lead_id = None
            if duplicate is not None and duplicate.lead is not None:
                duplicate_lead_id = duplicate.lead.id
                stats["duplicates"] += 1

            lead = s.scalar(select(Lead).where(Lead.post_id == post.id)) or Lead(
                post_id=post.id
            )
            lead.classification = result.classification
            lead.confidence = result.confidence
            lead.reason = result.reason
            lead.classifier_version = result.classifier_version
            lead.decided_by = result.decided_by
            lead.evidence_quote = result.evidence_quote
            lead.lead_score = score
            lead.priority = priority
            lead.score_breakdown = breakdown
            lead.duplicate_of_id = duplicate_lead_id

            extracted = result.extracted or {}
            lead.job_title = extracted.get("job_title")
            lead.skills = extracted.get("skills") or []
            lead.employment_type = extracted.get("employment_type")
            lead.hire_target = extracted.get("hire_target")
            lead.company = extracted.get("company")
            lead.budget = extracted.get("budget")
            lead.location = extracted.get("location")
            lead.urgency = extracted.get("urgency")
            s.add(lead)
            stats["leads"] += 1

    return stats
