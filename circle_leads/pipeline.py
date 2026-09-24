"""End-to-end pipeline: discover -> enrich -> validate -> ingest -> classify -> score.

Ingestion is gated on operator approval at three points: the permission file's
status, the per-space allowlist, and the DM exclusion in the scraper. A
community missing any of these yields zero collected items rather than an error.

Enrichment (``enrich_pending``) sits between discovery and ICP classification
because a community can only be judged on the text it has: discovery records
whatever the source happened to publish, which for most of the backlog is a URL
and nothing else, and every stage downstream reads a blank row as a rejected
one. It walks its own cursor rather than riding on harvest's ordering -- see the
function for why that distinction is the whole point.
"""

from __future__ import annotations

import html
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Iterable
from urllib.parse import urlparse

import requests
from sqlalchemy import or_, select

from circle_leads.authentication.browser_session import (
    CIRCLE_BASE,
    AdminCredentials,
    MemberSession,
    MissingCredentialError,
    NotAuthorizedError,
    mint_member_session,
    resolve_admin_credentials,
)
from circle_leads.classifier.ai_classifier import LlmBackend, make_backend
from circle_leads.classifier.icp_relevance import classify_icp_fit
from circle_leads.classifier.lead_classifier import classify, meets_requirements
from circle_leads.config.settings import CommunityPermission, Requirements
from circle_leads.discovery.discover_communities import DiscoveredCommunity
from circle_leads.discovery.join_type import _classify_payload
from circle_leads.discovery.validate_community import assess_relevance, check_public_access
from circle_leads.export.vini_ingest import push_leads_by_ids
from circle_leads.scoring.lead_scoring import score_lead
from circle_leads.scraper import chat_scraper, comments_scraper, community_scraper, posts_scraper
from circle_leads.scraper.governor import LOW, priority
from circle_leads.scraper.http_client import shared_session
from circle_leads.scraper.pagination import AccessDeniedError, ApiError, CircleClient, QuotaTracker
from circle_leads.scraper.public_reader import PublicReader
from circle_leads.storage.database import (
    Database,
    find_near_duplicate,
    get_or_create_author,
    get_or_create_community,
    get_or_create_space,
    live_original,
    retire_lead,
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
from circle_leads.storage.settings_store import get_setting, set_setting

logger = logging.getLogger(__name__)

# How many already-decided communities one scheduled pass may re-score. The
# sweep re-stamps icp_checked_at and selects oldest-stamp-first, so each run
# walks a different slice of the backlog instead of re-reading the same head
# forever; this bound is what stops an hourly worker from turning a 12k-row
# table into 12k LLM calls per hour.
ICP_RESCORE_BATCH = 200

# Total communities one *scheduled* (unattended) ICP pass may classify, across
# both selections together -- never-checked rows and re-scores. The re-score
# branch was bounded from the start, the never-checked branch was not, so a
# directory crawl or a big harvest could hand a single worker tick an arbitrary
# number of rows and, with a key set, an arbitrary number of LLM calls. Prod
# happened to have one unchecked row, which made that safe by data rather than
# by code. The cap is applied in classify_icp_pending whenever the caller asks
# for the scheduled behaviour (rescore_llm_eligible) without naming its own
# limit, so the worker cannot forget it.
ICP_SCHEDULED_BATCH = 200


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
        backend = make_backend()
        if backend is not None:
            llm, model_name = backend, getattr(backend, "model", "llm")
        else:
            logger.warning("Semantic classification requested but no LLM key is set.")

    stats = {"classified": 0, "leads": 0, "not_leads": 0, "duplicates": 0, "filtered": 0}
    pending_external_ids: list[int] = []

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
            # No model verdict (an outage, spent credits): the rules decided
            # alone -- enough to show a lead for review, not to retire an
            # earlier one or push this one to Vini.
            held = result.llm_error is not None

            if not result.is_lead:
                stats["not_leads"] += 1
                existing = s.scalar(select(Lead).where(Lead.post_id == post.id))
                if existing and not held:
                    retire_lead(s, existing, reason=result.reason,
                                decided_by=result.decided_by)
                continue

            # The confidence floor and role/skill filters always apply.
            # `exclude_job_seekers` governs job-seeker handling, not whether
            # requirements are enforced at all.
            if not meets_requirements(result, requirements):
                stats["filtered"] += 1
                # Drop any prior lead: after an edit the stored score, evidence
                # quote, and extracted fields describe text that is now gone.
                stale = s.scalar(select(Lead).where(Lead.post_id == post.id))
                if stale and not held:
                    retire_lead(s, stale, reason="filtered out by the target roles/skills "
                                f"or confidence floor ({result.reason})",
                                decided_by=result.decided_by)
                continue

            score, priority, breakdown = score_lead(
                result, requirements, published_at=post.published_at
            )

            duplicate = find_near_duplicate(s, post)
            duplicate_lead_id = None
            if duplicate is not None and live_original(duplicate.lead):
                duplicate_lead_id = duplicate.lead.id
                stats["duplicates"] += 1

            existing = s.scalar(select(Lead).where(Lead.post_id == post.id))
            if held and existing is not None:
                continue   # keep the verdict the model gave earlier
            lead = existing or Lead(post_id=post.id)

            # A lead once filed as a duplicate stays one. Re-judged on its full
            # text while the original is still a preview, the post no longer
            # looks near-identical, and dropping the link would push the same
            # lead to Vini a second time.
            if duplicate_lead_id is None and lead.duplicate_of_id not in (None, lead.id):
                if live_original(s.get(Lead, lead.duplicate_of_id)):
                    duplicate_lead_id = lead.duplicate_of_id
            lead.classification = result.classification
            lead.confidence = result.confidence
            lead.reason = result.reason
            if held:
                lead.reason = (f"{result.reason} Held for review, not sent to "
                               f"Vini: no LLM verdict ({result.llm_error}).")
            lead.classifier_version = result.classifier_version
            lead.decided_by = result.decided_by
            lead.evidence_quote = result.evidence_quote
            lead.lead_score = score
            lead.priority = priority
            # The model's description the lead rule decided on rides along in
            # the breakdown: it has no column, and a reviewer needs it to see
            # why the post was filed.
            lead.score_breakdown = (
                {**breakdown, "described": result.described}
                if result.described else breakdown
            )
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
            s.flush()
            if duplicate_lead_id is None and lead.external_synced_at is None and not held:
                pending_external_ids.append(lead.id)
            stats["leads"] += 1

    if pending_external_ids:
        with db.session() as s:
            push = push_leads_by_ids(s, pending_external_ids)
            if push.errors:
                logger.error(
                    "Vini ingest failed for %d lead(s): %s",
                    push.attempted,
                    "; ".join(push.errors[:3]),
                )
            elif push.sent:
                logger.info("Vini ingest sent %d lead(s)", push.sent)

    return stats


def _llm_eligible_icp_ids(s, *, limit: int) -> list[int]:
    """Already-decided communities worth spending an LLM call on.

    ``classify_icp_pending``'s default selection (icp_checked_at IS NULL) is a
    trap on a mature database: every row has been stamped once, so a scheduled
    sweep selects nothing forever and switching the LLM on changes nothing for
    the 12k rows already there. This is the re-score selection instead -- rows
    the *rules* decided, that were not flagged, and that have text to judge.

    It deliberately does not try to recompute the escalation band in SQL: the
    stored icp_score is clamped to [0, 100], so a row whose raw rules score was
    a decisive -30 is indistinguishable here from a genuinely ambiguous 0.
    ``classify_icp_fit`` re-derives the unclamped score and returns a rules
    verdict without calling the LLM for anything outside the band, so this only
    has to be a cheap pre-filter, not an exact one.

    Ordering by icp_checked_at is the cursor: each pass re-stamps the rows it
    touched, which sends them to the back of the queue and hands the next pass
    the next slice.
    """
    query = (
        select(Community.id)
        .where(
            Community.icp_checked_at.is_not(None),
            Community.icp_decided_by == "rules",
            # Never demote a row already in the join queue on an LLM's say-so,
            # and don't spend the batch's budget on rows the rules already
            # answered confidently -- classify_icp_fit would skip the LLM anyway.
            Community.icp_flag.is_(False),
            # A row with neither name nor description has nothing to judge; the
            # classifier's own empty-text guard would reject it again, so it
            # would consume a slot and produce no new information.
            or_(
                Community.name.is_not(None) & (Community.name != ""),
                Community.description.is_not(None) & (Community.description != ""),
            ),
        )
        .order_by(Community.icp_checked_at, Community.id)
        .limit(limit)
    )
    return list(s.scalars(query).all())


def classify_icp_pending(
    db: Database,
    requirements: Requirements,
    *,
    use_llm: bool = False,
    limit: int | None = None,
    recheck: bool = False,
    rescore_llm_eligible: bool = False,
    rescore_limit: int = ICP_RESCORE_BATCH,
    trust_llm_flags: bool = False,
) -> dict[str, int]:
    """Score discovered communities for ICP fit (classifier/icp_relevance.py).

    Gates the auto-join queue: only icp_flag=True communities are ever queued to
    join (see join/queue.py). By default only communities never checked are
    processed; ``recheck`` re-scores everything, e.g. after tuning the rules.

    ``rescore_llm_eligible`` additionally re-scores a bounded slice of rows that
    a *rules*-only run already decided (see ``_llm_eligible_icp_ids``). It is
    the scheduled path's way out of the no-op trap described there, and it is
    ignored unless an LLM backend actually materialized: without one, a
    re-score just re-runs identical rules over identical text and rewrites the
    identical verdict. Asking for it also opts into ``ICP_SCHEDULED_BATCH`` as
    the default total budget for the run (see the constant): it is the marker
    of the unattended path, and unattended work has to be bounded even when the
    caller passes no limit.

    ``trust_llm_flags`` lets an LLM-decided fit set icp_flag, which is what
    puts a community in front of the auto-join bot. Off by default and never on
    for the worker -- see classifier/icp_relevance.py's ``llm_may_flag``.
    """
    llm: LlmBackend | None = None
    model_name = None
    if use_llm:
        backend = make_backend()
        if backend is not None:
            llm, model_name = backend, getattr(backend, "model", "llm")
        else:
            logger.warning("ICP LLM escalation requested but no LLM key is set.")

    stats = {"checked": 0, "flagged": 0, "not_flagged": 0}

    # The scheduled path pays for its own ceiling: `limit` bounds the
    # never-checked selection in SQL and then truncates the combined list, so
    # one value caps both branches together.
    if rescore_llm_eligible and limit is None:
        limit = ICP_SCHEDULED_BATCH

    with db.session() as s:
        query = select(Community.id)
        if not recheck:
            query = query.where(Community.icp_checked_at.is_(None))
        query = query.order_by(Community.id)
        if limit:
            query = query.limit(limit)
        pending_ids = list(s.scalars(query).all())

        # Never-checked rows keep their priority: a freshly discovered
        # community should be judged before an old one is judged again.
        if rescore_llm_eligible and llm is not None and not recheck:
            already = set(pending_ids)
            pending_ids += [
                cid
                for cid in _llm_eligible_icp_ids(s, limit=max(0, rescore_limit))
                if cid not in already
            ]
            if limit:
                pending_ids = pending_ids[:limit]

    for community_pk in pending_ids:
        with db.session() as s:
            community = s.get(Community, community_pk)
            if community is None:
                continue

            goal = (community.directory_goals or [None])[0]
            result = classify_icp_fit(
                community.name,
                community.description,
                goal=goal,
                llm=llm,
                model_name=model_name,
                escalation_threshold=requirements.icp_escalation_threshold,
                llm_may_flag=trust_llm_flags,
            )
            community.icp_score = result.score
            community.icp_flag = result.flag
            community.icp_reasons = result.reasons
            community.icp_checked_at = utcnow()
            community.icp_decided_by = result.decided_by

            stats["checked"] += 1
            stats["flagged" if result.flag else "not_flagged"] += 1

    return stats


# --- Enrichment -------------------------------------------------------------

# Bulk requests to *.circle.so stay at 2-4 concurrent workers. Measured the hard
# way during the 2026-09 discovery expansion (see WORKLOG.md): at 12 threads,
# 72% of responses came back silently empty -- not a 429, not an error, just an
# empty body. The cap lives here rather than in the caller because that failure
# mode is invisible from the outside: a throttled run and a run over dead hosts
# produce exactly the same "no metadata" rows, and storing those is how a
# community gets permanently filed as unjudgeable.
ENRICH_MAX_WORKERS = 4
ENRICH_DEFAULT_WORKERS = 3

# Communities per scheduled enrichment pass. Two public requests each, at 3
# workers -- small enough to stay polite, large enough that the backlog drains
# in days rather than months.
ENRICH_BATCH = 200

# Where the backlog cursor lives (storage/settings_store.py): the highest
# community id this stage has already visited.
ENRICH_CURSOR_KEY = "enrichment_cursor_id"

# Circle serves a templated description for any community whose owner never
# wrote one. A previous enrichment run stored 232 such descriptions out of 329 --
# "Explore <space> space in <community>", "<community> community home page".
# That text is worse than no text: to the ICP classifier a boilerplate row looks
# like a row with real metadata, so it scores the template's words, records a
# confident rules verdict, and the community is never judged on anything it
# actually said about itself. The same applies to a bot-check interstitial's
# title, which describes the gate rather than the community behind it.
#
# Every pattern here has to match the *whole* templated string, not merely
# contain its words. Unanchored re.search was rejecting legitimate copy that
# happens to use the same English -- "Explore our space in Berlin for creative
# entrepreneurs.", "Just a moment of your time each week.", "Attention required
# for founders who ship." -- and a false positive here is permanent: enrichment
# discards the description, the row keeps looking empty, and the next
# enrichment pass fetches the same good text and discards it again.
_BOILERPLATE_PATTERNS = (
    # "Explore Members space in Acme Founders" / "Explore the General space in
    # Acme Founders": a space name, then the community name, and nothing else.
    # The template always names the space, so the lookahead requires a real
    # word there -- that is what separates it from prose like "Explore our
    # space in Berlin ..." or "Explore the space in between design and code."
    # The end anchor drops anything that carries on into a further clause.
    re.compile(r"^explore\s+(?:the\s+|an?\s+)?"
               r"(?!the\b|an?\b|our\b|my\b|your\b|this\b|space\b)"
               r"[^.]{1,60}?\bspace\s+in\s+[^.]{1,80}\.?$", re.I),
    # "<Community> community home page" -- the templated <title>, so the phrase
    # ends the string; "our community homepage has moved" does not.
    re.compile(r"\bcommunity home\s?page\s*[.!]?$", re.I),
    # Circle's footer credit on its own, not a description that mentions it.
    re.compile(r"^\W*powered by circle\W*$", re.I),
    # Cloudflare's interstitial title is exactly "Just a moment..." -- keep
    # "Just a moment of your time each week."
    re.compile(r"^just a moment[\s.\u2026!]*$", re.I),
    re.compile(r"^verifying you are (a )?human\b", re.I),
    # "Attention Required! | Cloudflare". The bang is the tell; "Attention
    # required for founders who ship." is a real description.
    re.compile(r"^attention required\s*!", re.I),
    # Only the challenge page's full instruction, not the two words.
    re.compile(r"\benable javascript and cookies to continue\b", re.I),
    # Circle's own auth-page meta description, templated per community:
    # "Login to <X> community via email or SSO today." It names the community
    # but says nothing about it, and it is worse than an empty field: storing
    # it marks the row as "has a description", so enrichment never revisits it
    # while the ICP classifier scores it as real text. Caught live on
    # venturerise during the first prod enrichment run.
    re.compile(r"^log ?in to\b.{0,120}?\bvia (email|sso)\b", re.I),
    re.compile(r"^(log ?in|sign ?in|sign ?up) to\b.{0,80}?\bcommunity\b", re.I),
    re.compile(r"^create an account or log ?in\b", re.I),
    # circle.so's own marketing <title>. A host with no community left behind it
    # falls through to the marketing site, so this is a dead host, not a name --
    # 738 DNS-import rows were stored with it as their name on 2026-09-18.
    re.compile(r"^circle\s*[|\-–—]\s*a new era for digital businesses\b", re.I),
)

# Exact titles that carry no information about the community at all.
_JUNK_NAMES = frozenset({
    "circle", "circle.so", "community", "home", "loading", "log in", "login",
    "redirecting", "sign in", "sign up", "untitled",
})

# "Log in | Acme", "Home | Acme", "Login – Acme": a page title wrapped around
# the community's name. The name is still in there; only the prefix is noise.
_PAGE_TITLE_PREFIX = re.compile(
    r"^\s*(?:log ?in|sign ?in|home|welcome)\s*[|\-–—:]\s*", re.I
)


def _clean_name(text: str | None) -> str | None:
    """Unescape HTML entities, drop a page-title prefix and a doubled title.

    Landing-page <title>s arrive HTML-escaped ("Founder&#39;s Circle") and
    often as "<page> | <site>" -- the part worth keeping is the community's
    own name, not the page it was read from.
    """
    if text is None:
        return None
    value = html.unescape(text).strip()
    value = _PAGE_TITLE_PREFIX.sub("", value, count=1).strip()
    # "Ulule Connect | Ulule Connect" -> "Ulule Connect".
    parts = [p.strip() for p in value.split("|")]
    if len(parts) == 2 and parts[0].lower() == parts[1].lower():
        value = parts[0]
    return value or None


def _is_boilerplate(text: str | None) -> bool:
    """True when this text says nothing specific about the community."""
    value = (text or "").strip()
    if not value:
        return True
    if value.lower().strip(" .|-") in _JUNK_NAMES:
        return True
    return any(pattern.search(value) for pattern in _BOILERPLATE_PATTERNS)


@dataclass
class CommunityMetadata:
    """What one enrichment fetch managed to establish about a community."""

    name: str | None = None
    description: str | None = None
    #: Fields that did come back but were rejected as boilerplate -- kept
    #: separate from "nothing came back" so a throttled run is still legible
    #: in the stats afterwards.
    rejected: list[str] = field(default_factory=list)
    note: str | None = None
    #: How the community can be joined, when communities/current answered 200
    #: -- the name request already carries it, so no separate probe is needed.
    join_type: str | None = None
    join_detail: str | None = None


def fetch_community_metadata(
    host: str,
    url: str,
    *,
    session: requests.Session | None = None,
    want_name: bool = True,
    want_description: bool = True,
) -> CommunityMetadata:
    """Read one community's public name and description. Never raises.

    Two readers that already exist, cheapest first:
    ``PublicReader.community_name()`` asks ``/internal_api/communities/current``
    -- the same unauthenticated JSON API the join-type probe uses, and the one
    that still answers when the marketing page serves a bot check -- and
    ``check_public_access()`` reads the landing page's <title> and meta
    description, which is the only one of the two that yields a description.
    """
    http = session or shared_session()
    meta = CommunityMetadata()

    if want_name:
        reader = PublicReader(host, session=http)
        name = _clean_name(reader.community_name())
        if name:
            if _is_boilerplate(name):
                meta.rejected.append("name")
            else:
                meta.name = name
        payload = getattr(reader, "last_payload", None)
        if getattr(reader, "last_status", None) == 200 and isinstance(payload, dict) and "is_private" in payload:
            join = _classify_payload(payload)
            meta.join_type, meta.join_detail = join.join_type, join.detail
        if getattr(reader, "last_status", None) == 0:
            # The host didn't answer at all (DNS, TLS handshake, timeout): its
            # landing page lives on the same host and would fail the same way.
            meta.note = "host unreachable"
            return meta

    # Only pay for the landing page when it can still tell us something.
    if want_description or (want_name and meta.name is None):
        check = check_public_access(url, session=http)
        if want_description and check.description:
            if _is_boilerplate(check.description):
                meta.rejected.append("description")
            else:
                meta.description = check.description
        page_name = _clean_name(check.name)
        if want_name and meta.name is None and page_name:
            if _is_boilerplate(page_name):
                if "name" not in meta.rejected:
                    meta.rejected.append("name")
            else:
                meta.name = page_name
        if meta.name is None and meta.description is None and not meta.rejected:
            meta.note = check.note or f"HTTP {check.http_status}"

    return meta


def _enrich_cursor(db: Database) -> int:
    raw = get_setting(db, ENRICH_CURSOR_KEY, "0")
    try:
        return max(0, int(raw or 0))
    except (TypeError, ValueError):  # a hand-edited setting must not brick the stage
        return 0


def enrich_pending(
    db: Database,
    *,
    limit: int | None = None,
    max_workers: int = ENRICH_DEFAULT_WORKERS,
    fetch: Callable[..., CommunityMetadata] = fetch_community_metadata,
) -> dict[str, int]:
    """Fill in missing name/description, walking the backlog oldest-first.

    A stage of its own, with a cursor of its own, deliberately not folded into
    the harvest: harvest's read stage is capped at 150 communities ordered by
    (watching DESC, relevance_score DESC), and ~195 rows permanently occupy that
    head -- so a row imported from a file weeks ago is never reached, no matter
    how many times the harvest runs. This walks by ascending id instead (oldest
    row first) and remembers where it stopped, so the backlog drains at a fixed
    cost per run and every row is eventually visited exactly once per lap.

    This is what gates the rest of the funnel: communities with text convert to
    ICP fit at 16.2%, communities without convert at 0%, and 9,941 of 12,476
    rows have neither a name nor a description. Those are not bad candidates --
    they are unjudged ones.
    """
    batch = ENRICH_BATCH if limit is None else max(0, limit)
    # Clamp rather than trust: see ENRICH_MAX_WORKERS.
    workers = max(1, min(int(max_workers), ENRICH_MAX_WORKERS))
    cursor = _enrich_cursor(db)
    stats = {
        "visited": 0, "named": 0, "described": 0, "boilerplate_rejected": 0,
        "nothing_found": 0, "skipped": 0, "wrapped": 0, "cursor": cursor,
    }
    if batch == 0:
        return stats

    with db.session() as s:
        rows = list(
            s.execute(
                select(
                    Community.id, Community.url, Community.name, Community.description
                )
                .where(
                    Community.id > cursor,
                    or_(
                        Community.name.is_(None),
                        Community.name == "",
                        Community.description.is_(None),
                        Community.description == "",
                    ),
                )
                .order_by(Community.id)
                .limit(batch)
            ).all()
        )

    if not rows:
        # End of the backlog. Restart from the top next run so rows that were
        # unreachable this lap (host down, bot check, a slug that only resolved
        # later) get another chance without anyone scheduling anything.
        if cursor:
            set_setting(db, ENRICH_CURSOR_KEY, "0")
            stats["wrapped"] = 1
            stats["cursor"] = 0
        return stats

    targets = []
    for community_pk, url, name, description in rows:
        host = urlparse(url if "://" in (url or "") else f"https://{url or ''}").hostname
        # A discover.circle.so row is a directory *listing*, not a community
        # host: fetching it returns the marketplace page (behind Cloudflare),
        # never this community's own metadata. Skipped, but still walked past.
        if not host or host.lower().endswith("discover.circle.so"):
            stats["skipped"] += 1
            continue
        targets.append(
            (
                community_pk,
                host,
                url,
                not (name or "").strip(),
                not (description or "").strip(),
            )
        )

    results: dict[int, CommunityMetadata] = {}
    if targets:
        http = shared_session()

        def _one(target):
            with priority(LOW):  # yields Circle's per-IP budget to lead reads
                return _fetch_one(target)

        def _fetch_one(target):
            community_pk, host, url, want_name, want_description = target
            try:
                return community_pk, fetch(
                    host,
                    url,
                    session=http,
                    want_name=want_name,
                    want_description=want_description,
                )
            except Exception as exc:  # noqa: BLE001 - one dead host must not end the pass
                logger.debug(
                    "Enrichment failed for %s: %s", host, exc.__class__.__name__
                )
                return community_pk, CommunityMetadata(
                    note=f"{exc.__class__.__name__}: {exc}"[:200]
                )

        with ThreadPoolExecutor(max_workers=workers) as pool:
            results = dict(pool.map(_one, targets))

    # One transaction for the write-back: every request is already done, so
    # there is nothing left to interleave with and no reason to pay per-row.
    with db.session() as s:
        for community_pk, meta in results.items():
            community = s.get(Community, community_pk)
            if community is None:
                continue
            stats["visited"] += 1
            if meta.name and not (community.name or "").strip():
                community.name = meta.name[:512]
                stats["named"] += 1
            if meta.description and not (community.description or "").strip():
                community.description = meta.description[:4000]
                stats["described"] += 1
            if meta.join_type and community.join_type_checked_at is None:
                community.join_type = meta.join_type
                community.join_type_detail = (meta.join_detail or "")[:2000]
                community.join_type_checked_at = utcnow()
                stats["join_typed"] = stats.get("join_typed", 0) + 1
            stats["boilerplate_rejected"] += len(meta.rejected)
            if not meta.name and not meta.description:
                stats["nothing_found"] += 1

    last_id = rows[-1][0]
    set_setting(db, ENRICH_CURSOR_KEY, str(last_id))
    stats["cursor"] = last_id
    return stats
