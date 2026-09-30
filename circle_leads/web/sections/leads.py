"""Section 3: every lead we hold, split by what Vini said about it.

Segments (common.segment_expr): accepted, duplicate (Vini already had it),
held (parked -- the client does not see it), rejected, error (no answer),
not_sent (with our own reason), and no_data -- sent before Vini's answers
were kept, so what it said is unknown.

The counts ignore the segment filter, so the tabs always show the whole split
of whatever else is filtered.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from circle_leads.storage.models import Author, Community, Lead, Post
from circle_leads.web.sections.common import (
    NOT_SENT_REASONS, SEGMENTS, not_sent_reason_expr, segment_expr,
)
from circle_leads.web.sections.deps import (
    get_db, iso, local_day_to_utc, require_session, tz_offset,
)

router = APIRouter(prefix="/api/dash", dependencies=[Depends(require_session)])

SORTS = {
    "found": (Lead.created_at.desc(), Lead.id.desc()),
    "score": (Lead.lead_score.desc(), Lead.id.desc()),
    "published": (Post.published_at.desc(), Lead.id.desc()),
}
SNIPPET_CHARS = 280


def _filters(*, q: str | None, community_id: int | None, classification: str | None,
             since: datetime | None, until: datetime | None, reason: str | None) -> list:
    conds: list = []
    if classification in ("LEAD", "NOT_LEAD", "UNCERTAIN"):
        conds.append(Lead.classification == classification)
    if community_id:
        conds.append(Post.community_id == community_id)
    if since is not None:
        conds.append(Lead.created_at >= since)
    if until is not None:
        conds.append(Lead.created_at < until)
    if q:
        like = f"%{q.strip()}%"
        conds.append(or_(
            Post.content.ilike(like), Post.title.ilike(like), Lead.job_title.ilike(like),
            Lead.company.ilike(like), Author.display_name.ilike(like),
            Community.name.ilike(like), Community.slug.ilike(like),
        ))
    if reason:
        if reason in NOT_SENT_REASONS:
            conds.append(not_sent_reason_expr() == reason)
        else:
            conds.append(Lead.vini_reason == reason)
    return conds


def _base(select_stmt):
    return (select_stmt
            .join(Post, Lead.post_id == Post.id)
            .join(Community, Post.community_id == Community.id)
            .outerjoin(Author, Post.author_id == Author.id))


def query_leads_page(
    s: Session, *, segments: list[str] | None = None, q: str | None = None,
    community_id: int | None = None, classification: str | None = None,
    since: datetime | None = None, until: datetime | None = None,
    reason: str | None = None, sort: str = "found", page: int = 1, limit: int = 50,
) -> dict[str, Any]:
    if sort not in SORTS:
        raise ValueError(f"sort must be one of {sorted(SORTS)}")
    seg = segment_expr()
    # The tabs and the reason chips ignore the chosen reason: they are how
    # the reader moves to another one.
    conds = _filters(q=q, community_id=community_id, classification=classification,
                     since=since, until=until, reason=None)
    reason_cond = _filters(q=None, community_id=None, classification=None, since=None,
                           until=None, reason=reason)

    counts = {name: 0 for name in SEGMENTS}
    for name, n in s.execute(_base(select(seg, func.count(Lead.id)))
                             .where(*conds).group_by(seg)).all():
        counts[name] = int(n)
    counts["all"] = sum(counts[name] for name in SEGMENTS)

    # What the reason chips offer: Vini's own words for the answers that are
    # not "accepted", and our reason for the leads that never went out.
    nsr = not_sent_reason_expr()
    reasons = {
        "vini": [
            {"segment": st, "reason": r or "", "count": int(n)}
            for st, r, n in s.execute(
                _base(select(Lead.vini_status, Lead.vini_reason, func.count(Lead.id)))
                .where(*conds, Lead.vini_status.in_(("held", "rejected", "error")))
                .group_by(Lead.vini_status, Lead.vini_reason)
                .order_by(func.count(Lead.id).desc()).limit(30)
            ).all()
        ],
        "not_sent": [
            {"reason": r, "count": int(n)}
            for r, n in s.execute(
                _base(select(nsr, func.count(Lead.id)))
                .where(*conds, seg == "not_sent").group_by(nsr)
                .order_by(func.count(Lead.id).desc())
            ).all()
        ],
    }

    where = [*conds, *reason_cond]
    if segments:
        where.append(seg.in_(segments))
    if reason_cond:
        filtered = s.scalar(_base(select(func.count(Lead.id))).where(*where)) or 0
    else:
        filtered = sum(counts[name] for name in (segments or SEGMENTS))
    limit = max(1, min(limit, 200))
    page = max(1, page)
    rows = s.execute(
        _base(select(
            Lead, seg.label("segment"), nsr.label("not_sent_reason"),
            Post.url, Post.title, Post.published_at, Post.content,
            Author.display_name, Community.id.label("community_id"),
            Community.slug, Community.name.label("community_name"),
            Community.url.label("community_url"),
        ))
        .where(*where)
        .order_by(*SORTS[sort])
        .offset((page - 1) * limit).limit(limit)
    ).all()
    return {
        "counts": counts,
        "reasons": reasons,
        "filtered": filtered,
        "page": page,
        "limit": limit,
        "rows": [_row(r) for r in rows],
    }


def _row(r) -> dict[str, Any]:
    lead: Lead = r.Lead
    content = (r.content or "").strip()
    return {
        "id": lead.id,
        "created_at": iso(lead.created_at),
        "published_at": iso(r.published_at),
        "classification": lead.classification,
        "decided_by": lead.decided_by,
        "lead_score": lead.lead_score,
        "priority": lead.priority,
        "job_title": lead.job_title,
        "company": lead.company,
        "segment": r.segment,
        "not_sent_reason": r.not_sent_reason if r.segment == "not_sent" else None,
        "vini_status": lead.vini_status,
        "vini_reason": lead.vini_reason,
        "vini_ref": lead.vini_ref,
        "vini_attempts": lead.vini_attempts or 0,
        "vini_last_attempt_at": iso(lead.vini_last_attempt_at),
        "external_synced_at": iso(lead.external_synced_at),
        "duplicate_of_id": lead.duplicate_of_id,
        "author": r.display_name,
        "post_url": r.url,
        "title": r.title,
        "snippet": content[:SNIPPET_CHARS] + ("…" if len(content) > SNIPPET_CHARS else ""),
        # What the lead card shows (the first dashboard's design): the whole
        # post, clamped on the page, and the facts the model pulled out of it.
        "content": content,
        "reason": lead.reason,
        "confidence": lead.confidence,
        "skills": lead.skills or [],
        "employment_type": lead.employment_type,
        "hire_target": lead.hire_target,
        "budget": lead.budget,
        "location": lead.location,
        "urgency": lead.urgency,
        "community": {"id": r.community_id, "slug": r.slug,
                      "name": r.community_name or r.slug, "url": r.community_url},
    }


def lead_detail(s: Session, lead_id: int) -> dict[str, Any] | None:
    row = s.execute(
        _base(select(
            Lead, segment_expr().label("segment"), not_sent_reason_expr().label("not_sent_reason"),
            Post.url, Post.title, Post.published_at, Post.content,
            Author.display_name, Community.id.label("community_id"),
            Community.slug, Community.name.label("community_name"),
            Community.url.label("community_url"),
        )).where(Lead.id == lead_id)
    ).first()
    if row is None:
        return None
    lead: Lead = row.Lead
    out = _row(row)
    out.update({
        "content": row.content,
        "evidence_quote": lead.evidence_quote,
        "score_breakdown": lead.score_breakdown or {},
        "classifier_version": lead.classifier_version,
        "vini_responded_at": iso(lead.vini_responded_at),
        "duplicates": [
            {"id": d_id, "created_at": iso(d_at)}
            for d_id, d_at in s.execute(
                select(Lead.id, Lead.created_at).where(Lead.duplicate_of_id == lead_id)
                .order_by(Lead.id)
            ).all()
        ],
    })
    return out


@router.get("/leads")
def api_leads(
    db=Depends(get_db), tz: int = Depends(tz_offset),
    segment: str | None = None, q: str | None = None,
    community_id: int | None = None, classification: str | None = None,
    since: str | None = None, until: str | None = None, reason: str | None = None,
    sort: str = "found", page: int = Query(1, ge=1), limit: int = Query(50, ge=1, le=200),
) -> dict[str, Any]:
    segments = [x for x in (segment or "").split(",") if x]
    unknown = set(segments) - set(SEGMENTS)
    if unknown:
        raise HTTPException(400, f"Unknown segment(s): {sorted(unknown)}")
    start = local_day_to_utc(since, tz, name="since")
    end = local_day_to_utc(until, tz, name="until")
    if end is not None:
        end += timedelta(days=1)  # "until" is inclusive: the whole day
    try:
        with db.session() as s:
            return query_leads_page(
                s, segments=segments or None, q=q, community_id=community_id,
                classification=classification, since=start, until=end, reason=reason,
                sort=sort, page=page, limit=limit,
            )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/leads/{lead_id}")
def api_lead(lead_id: int, db=Depends(get_db)) -> dict[str, Any]:
    with db.session() as s:
        out = lead_detail(s, lead_id)
    if out is None:
        raise HTTPException(404, "Lead not found")
    return out
