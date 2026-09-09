"""Triage pasted community text into ranked leads.

This is the path for communities you have joined as an ordinary member: you
read the pages you are entitled to read, paste what you see, and the tool does
the judgement -- separating people who want to hire from people who want a job,
extracting the role and budget, scoring, and deduplicating against everything
you have triaged before.

No network calls, no credentials, no API. Only text you supply.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select

from circle_leads.classifier.ai_classifier import LlmBackend, make_backend
from circle_leads.classifier.lead_classifier import classify, meets_requirements
from circle_leads.config.settings import Requirements
from circle_leads.export.vini_ingest import push_leads_by_ids
from circle_leads.scoring.lead_scoring import score_lead
from circle_leads.storage.activity import log_activity
from circle_leads.storage.database import (
    Database,
    content_hash,
    find_near_duplicate,
    get_or_create_author,
    get_or_create_community,
    upsert_post,
)
from circle_leads.storage.models import AccessState, Lead, PermissionStatus, Post
from circle_leads.triage.reply import draft_reply
from circle_leads.triage.splitter import RawPost, split_posts

logger = logging.getLogger(__name__)

# Content triaged by hand is recorded as such, so it is never confused with
# API-collected content and never implies an operator approved anything.
TRIAGE_SOURCE = "manual_triage"


@dataclass
class TriageResult:
    total_posts: int = 0
    leads: list[dict] = field(default_factory=list)
    not_leads: int = 0
    filtered: int = 0
    duplicates: int = 0
    already_seen: int = 0
    too_old: int = 0

    @property
    def new_leads(self) -> list[dict]:
        return [lead for lead in self.leads if not lead.get("is_duplicate")]


def _relative_to_datetime(label: str | None) -> datetime | None:
    """Best-effort parse of "2h ago" / "yesterday" into a timestamp."""
    if not label:
        return None
    import re

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    text = label.lower().strip()

    if "just now" in text or "today" in text:
        return now
    if "yesterday" in text:
        from datetime import timedelta

        return now - timedelta(days=1)

    m = re.match(
        r"(\d+)\s*(s|m|h|d|w|mo|y|sec|min|hour|day|week|month|year)", text
    )
    if not m:
        return None
    from datetime import timedelta

    n, unit = int(m.group(1)), m.group(2)
    scale = {
        "s": timedelta(seconds=1), "sec": timedelta(seconds=1),
        "m": timedelta(minutes=1), "min": timedelta(minutes=1),
        "h": timedelta(hours=1), "hour": timedelta(hours=1),
        "d": timedelta(days=1), "day": timedelta(days=1),
        "w": timedelta(weeks=1), "week": timedelta(weeks=1),
        "mo": timedelta(days=30), "month": timedelta(days=30),
        "y": timedelta(days=365), "year": timedelta(days=365),
    }.get(unit)
    return now - (scale * n) if scale else None


def triage_records(
    db: Database,
    records: list[dict],
    requirements: Requirements,
    *,
    community: str = "manual",
    space: str | None = None,
    source_url: str | None = None,
    use_llm: bool = False,
    your_name: str | None = None,
    verbose_log: bool = False,
) -> TriageResult:
    """Classify already-structured post records (one per post, no splitting).

    For callers that read a feed and have clean per-post records -- avoids the
    text splitter, which merges posts when records are joined then re-split.
    Each record carries at least ``content``; optionally ``title`` and
    ``author.display_name`` / ``published_at``.
    """
    posts = [
        RawPost(
            content=(
                # Don't double the title if content already starts with it.
                r["content"]
                if not r.get("title") or r["content"].lstrip().startswith(r["title"])
                else r["title"] + "\n" + r["content"]
            ),
            author=(r.get("author") or {}).get("display_name"),
            index=i,
            meta={"url": r.get("url")},
        )
        for i, r in enumerate(records)
        if (r.get("content") or "").strip()
    ]
    published_map = {
        i: r.get("published_at") for i, r in enumerate(records)
    }
    return _triage_posts(
        db, posts, requirements, community=community, space=space,
        source_url=source_url, use_llm=use_llm, your_name=your_name,
        published_override=published_map, verbose_log=verbose_log,
    )


def triage_text(
    db: Database,
    text: str,
    requirements: Requirements,
    *,
    community: str = "manual",
    space: str | None = None,
    source_url: str | None = None,
    use_llm: bool = False,
    your_name: str | None = None,
) -> TriageResult:
    """Split, classify, score and store pasted community text."""
    posts: list[RawPost] = split_posts(text)
    return _triage_posts(
        db, posts, requirements, community=community, space=space,
        source_url=source_url, use_llm=use_llm, your_name=your_name,
    )


def _triage_posts(
    db: Database,
    posts: list[RawPost],
    requirements: Requirements,
    *,
    community: str = "manual",
    space: str | None = None,
    source_url: str | None = None,
    use_llm: bool = False,
    your_name: str | None = None,
    published_override: dict | None = None,
    verbose_log: bool = False,
) -> TriageResult:
    """Shared classify/score/store loop for split or structured posts."""
    result = TriageResult(total_posts=len(posts))
    if not posts:
        return result

    # Hard age cap (last line of defence -- both the public harvest and private
    # cookie scans land here). A post older than this is never stored,
    # classified, or filed. Undated posts pass: we can't prove they're old.
    age_cutoff = None
    if getattr(requirements, "max_post_age_days", 0) and requirements.max_post_age_days > 0:
        age_cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(
            days=requirements.max_post_age_days
        )

    llm: LlmBackend | None = None
    model_name = None
    if use_llm:
        backend = make_backend()
        if backend is not None:
            llm, model_name = backend, getattr(backend, "model", "llm")
        else:
            logger.warning("Semantic classification requested but no LLM key is set.")

    pending_external_ids: list[int] = []

    with db.session() as s:
        comm = get_or_create_community(
            s,
            slug=community,
            url=source_url or f"manual://{community}",
            discovery_source=TRIAGE_SOURCE,
        )
        # Manual triage is not operator-approved ingestion; keep the states
        # honest so `communities` never implies permission that was not given.
        comm.access_status = AccessState.VISITED.value
        if comm.permission_status == PermissionStatus.CANDIDATE.value:
            comm.permission_status = PermissionStatus.CANDIDATE.value
        community_pk = comm.id

    for raw in posts:
        published = _relative_to_datetime(raw.posted_label)
        if published_override and raw.index in published_override:
            override = published_override[raw.index]
            if override is not None:
                published = override

        if age_cutoff is not None and published is not None:
            when = published
            if getattr(when, "tzinfo", None) is not None:
                when = when.astimezone(timezone.utc).replace(tzinfo=None)
            if when < age_cutoff:
                result.too_old += 1
                continue

        with db.session() as s:
            author = get_or_create_author(
                s,
                community_id=community_pk,
                source_author_id=None,
                display_name=raw.author,
            )
            # Identity is the content itself: pasting the same screen twice
            # must not create a second lead.
            record = {
                "source_content_id": f"triage:{content_hash(raw.content)[:24]}",
                "content_type": "post",
                "content": raw.content,
                "title": None,
                "url": (raw.meta or {}).get("url") or source_url,
                "published_at": published,
                "author_id": author.id if author else None,
                "permission_reference": TRIAGE_SOURCE,
            }
            post, outcome = upsert_post(s, community_id=community_pk, record=record)
            post_pk = post.id

            if outcome == "unchanged" and post.classified:
                result.already_seen += 1
                continue

            classification = classify(
                post.content, requirements, llm=llm, model_name=model_name
            )
            post.classified = True

            if verbose_log:
                # Per-post decision trail: which layer decided, and why.
                preview = " ".join(post.content.split())[:70]
                matched = ", ".join(
                    (classification.hiring_matches or [])[:3]
                ) or "none"
                log_activity(
                    s,
                    kind="classify",
                    level="success" if classification.is_lead else "info",
                    community=community,
                    space=space,
                    summary=(
                        f"{classification.classification} "
                        f"(via {classification.decided_by}): {preview}"
                    ),
                    detail={
                        "decided_by": classification.decided_by,
                        "rule_score": classification.rule_score,
                        "confidence": round(classification.confidence, 2),
                        "hiring_patterns": matched,
                        "reason": (classification.reason or "")[:120],
                    },
                    decided_by=classification.decided_by,
                )

            if not classification.is_lead:
                result.not_leads += 1
                stale = s.scalar(select(Lead).where(Lead.post_id == post_pk))
                if stale:
                    s.delete(stale)
                continue

            if not meets_requirements(classification, requirements):
                result.filtered += 1
                stale = s.scalar(select(Lead).where(Lead.post_id == post_pk))
                if stale:
                    s.delete(stale)
                continue

            score, priority, breakdown = score_lead(
                classification, requirements, published_at=published
            )
            duplicate = find_near_duplicate(s, post)
            duplicate_lead_id = (
                duplicate.lead.id if duplicate is not None and duplicate.lead else None
            )
            if duplicate_lead_id:
                result.duplicates += 1

            extracted = classification.extracted or {}
            lead = s.scalar(select(Lead).where(Lead.post_id == post_pk)) or Lead(
                post_id=post_pk
            )
            lead.classification = classification.classification
            lead.confidence = classification.confidence
            lead.reason = classification.reason
            lead.classifier_version = classification.classifier_version
            lead.decided_by = classification.decided_by
            lead.evidence_quote = classification.evidence_quote
            lead.lead_score = score
            lead.priority = priority
            lead.score_breakdown = breakdown
            lead.duplicate_of_id = duplicate_lead_id
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
            # New non-duplicate leads go to production; already-synced rows are
            # skipped inside the push helper.
            if duplicate_lead_id is None and lead.external_synced_at is None:
                pending_external_ids.append(lead.id)

            payload: dict[str, Any] = {
                "community": community,
                "space": space,
                "author": raw.author,
                "content": post.content,
                "classification": lead.classification,
                "confidence": round(lead.confidence, 3),
                "lead_score": score,
                "priority": priority,
                "job_title": lead.job_title,
                "skills": lead.skills,
                "employment_type": lead.employment_type,
                "hire_target": lead.hire_target,
                "company": lead.company,
                "budget": lead.budget,
                "location": lead.location,
                "urgency": lead.urgency,
                "evidence_quote": lead.evidence_quote,
                "reason": lead.reason,
                "decided_by": lead.decided_by,
                "posted_label": raw.posted_label,
                "published_at": published.isoformat() if published else None,
                "url": (raw.meta or {}).get("url") or source_url,
                "is_duplicate": duplicate_lead_id is not None,
            }

        with db.session() as s:
            log_activity(
                s,
                kind="classify",
                level="success",
                community=community,
                space=space,
                summary=(
                    f"LEAD ({score}, {priority}): "
                    f"{payload.get('job_title') or payload.get('hire_target') or 'unspecified role'}"
                ),
                detail={
                    "author": raw.author,
                    "confidence": payload["confidence"],
                    "evidence": payload.get("evidence_quote"),
                    "reason": payload.get("reason"),
                    "skills": ", ".join(payload.get("skills") or []),
                    "budget": payload.get("budget"),
                },
                items_seen=1,
                leads_found=1,
                decided_by=payload.get("decided_by"),
            )

        draft = draft_reply(payload, your_name=your_name)
        payload["reply_draft"] = draft.text
        payload["reply_notes"] = draft.notes
        result.leads.append(payload)

    result.leads.sort(key=lambda x: x["lead_score"], reverse=True)

    if pending_external_ids:
        with db.session() as s:
            push = push_leads_by_ids(s, pending_external_ids)
            if push.sent or push.errors:
                log_activity(
                    s,
                    kind="export",
                    level="error" if push.errors else "success",
                    community=community,
                    summary=(
                        f"Vini ingest: sent {push.sent}/{push.attempted} lead(s)"
                        + (f" — {push.errors[0]}" if push.errors else "")
                    ),
                    detail={
                        "sent": push.sent,
                        "attempted": push.attempted,
                        "skipped": push.skipped,
                        "errors": "; ".join(push.errors[:3]),
                    },
                    leads_found=push.sent,
                )

    with db.session() as s:
        log_activity(
            s,
            kind="triage",
            level="success" if result.leads else "info",
            community=community,
            space=space,
            summary=(
                f"Triaged {result.total_posts} post(s): {len(result.leads)} lead(s), "
                f"{result.not_leads} not-lead, {result.filtered} filtered, "
                f"{result.too_old} too old, {result.already_seen} seen before"
            ),
            detail={
                "source_url": source_url,
                "llm": "on" if llm is not None else "off",
                "duplicates": result.duplicates,
                "too_old": result.too_old,
            },
            items_seen=result.total_posts,
            leads_found=len(result.leads),
            decided_by="llm" if llm is not None else "rules",
        )

    return result
