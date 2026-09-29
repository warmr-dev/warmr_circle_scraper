"""Section 4: every community in the database, searchable, filterable, paged
on the server (there are ~14k rows), plus the detail drawer both this section
and the monitoring section open.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.orm import Session, aliased

from circle_leads.reach import has_session
from circle_leads.storage.models import (
    ActivityLog, CircleConnection, Community, JoinFormFill, Lead, Post, ReplaySession,
    WatchState,
)
from circle_leads.web.overview import connection_bucket, source_bucket
from circle_leads.web.sections.common import (
    fit, has_access, lead_live, not_sent_reason_expr, segment_expr,
)
from circle_leads.web.sections.deps import cached, get_db, iso, require_session

router = APIRouter(prefix="/api/dash", dependencies=[Depends(require_session)])

NULL = "__null__"
MAX_LIMIT = 200


def _lead_totals():
    return (
        select(Post.community_id.label("community_id"), func.count(Lead.id).label("leads"))
        .join(Lead, Lead.post_id == Post.id)
        .where(lead_live())
        .group_by(Post.community_id)
        .subquery()
    )


def _eq_or_null(column, value: str | None):
    if value is None or value == "":
        return None
    if value == NULL:
        return column.is_(None)
    return column == value


def _filters(*, q, platform, icp, join_type, join_status, read_outcome, monitored,
             source, access) -> list:
    c = Community
    conds = [cond for cond in (
        _eq_or_null(c.platform, platform),
        _eq_or_null(c.join_type, join_type),
        _eq_or_null(c.join_status, join_status),
        _eq_or_null(c.read_outcome, read_outcome),
    ) if cond is not None]
    if q:
        like = f"%{q.strip()}%"
        conds.append(or_(c.name.ilike(like), c.slug.ilike(like), c.host.ilike(like),
                         c.url.ilike(like), c.description.ilike(like)))
    if icp == "fit":
        conds.append(fit())
    elif icp == "flag":
        conds.append(c.icp_flag.is_(True))
    elif icp == "no":
        conds.append(and_(or_(c.icp_flag.is_(False), c.icp_flag.is_(None)),
                          c.icp_checked_at.is_not(None)))
    elif icp == "unchecked":
        conds.append(c.icp_checked_at.is_(None))
    # An alias: the rows query joins watch_state itself, and a subquery on
    # the same table would be correlated away to nothing.
    w = aliased(WatchState)
    if monitored == "1":
        conds.append(exists(select(w.id).where(w.community_id == c.id, w.mode != "off")))
    elif monitored == "off":
        conds.append(exists(select(w.id).where(w.community_id == c.id, w.mode == "off")))
    elif monitored == "0":
        conds.append(~exists(select(w.id).where(w.community_id == c.id)))
    if source in ("directory", "dns", "other"):
        conds.append(source_bucket() == source)
    if access == "1":
        conds.append(has_access())
    elif access == "0":
        conds.append(~has_access())
    return conds


def query_communities(
    s: Session, *, q: str | None = None, platform: str | None = None,
    icp: str | None = None, join_type: str | None = None, join_status: str | None = None,
    read_outcome: str | None = None, monitored: str | None = None,
    source: str | None = None, access: str | None = None,
    sort: str = "icp", direction: str = "desc", page: int = 1, limit: int = 50,
) -> dict[str, Any]:
    c = Community
    totals = _lead_totals()
    sorts = {
        "icp": c.icp_score,
        "name": func.lower(func.coalesce(c.name, c.slug)),
        "discovered": c.discovered_at,
        "leads": func.coalesce(totals.c.leads, 0),
        "joined": c.joined_at,
        "read": c.last_synced_at,
    }
    if sort not in sorts:
        raise ValueError(f"sort must be one of {sorted(sorts)}")
    column = sorts[sort]
    order = column.asc() if direction == "asc" else column.desc()
    conds = _filters(q=q, platform=platform, icp=icp, join_type=join_type,
                     join_status=join_status, read_outcome=read_outcome,
                     monitored=monitored, source=source, access=access)

    total = s.scalar(select(func.count(c.id)).where(*conds)) or 0
    limit = max(1, min(limit, MAX_LIMIT))
    page = max(1, page)
    rows = s.execute(
        select(
            c.id, c.slug, c.name, c.host, c.url, c.platform, c.icp_flag, c.icp_score,
            c.join_type, c.join_status, c.read_outcome, c.discovered_at,
            c.discovery_source, c.price_label, c.last_synced_at, c.joined_at,
            fit().label("fit"), has_session().label("has_session"),
            WatchState.mode.label("watch_mode"), WatchState.tier.label("watch_tier"),
            func.coalesce(totals.c.leads, 0).label("leads"),
        )
        .outerjoin(WatchState, WatchState.community_id == c.id)
        .outerjoin(totals, totals.c.community_id == c.id)
        .where(*conds)
        .order_by(order.nulls_last(), c.id)
        .offset((page - 1) * limit).limit(limit)
    ).all()
    return {
        "total": int(total),
        "page": page,
        "limit": limit,
        "rows": [{
            "id": r.id, "slug": r.slug, "name": r.name or r.slug, "host": r.host,
            "url": r.url, "platform": r.platform, "icp_flag": bool(r.icp_flag),
            "icp_score": r.icp_score, "fit": bool(r.fit), "join_type": r.join_type,
            "join_status": r.join_status, "read_outcome": r.read_outcome,
            "discovered_at": iso(r.discovered_at), "discovery_source": r.discovery_source,
            "price_label": r.price_label, "last_synced_at": iso(r.last_synced_at),
            "joined_at": iso(r.joined_at), "has_session": bool(r.has_session),
            "watch_mode": r.watch_mode, "watch_tier": r.watch_tier, "leads": int(r.leads),
        } for r in rows],
    }


def community_facets(s: Session) -> dict[str, list[dict[str, Any]]]:
    """How many rows each filter value has, for the filter menus."""
    c = Community
    out: dict[str, list[dict[str, Any]]] = {}
    for key, column in (("platform", c.platform), ("join_type", c.join_type),
                        ("join_status", c.join_status), ("read_outcome", c.read_outcome)):
        out[key] = [
            {"value": value if value is not None else NULL, "count": int(n)}
            for value, n in s.execute(
                select(column, func.count(c.id)).group_by(column)
                .order_by(func.count(c.id).desc())
            ).all()
        ]
    bucket = source_bucket()
    out["source"] = [
        {"value": value, "count": int(n)}
        for value, n in s.execute(select(bucket, func.count(c.id)).group_by(bucket)).all()
    ]
    return out


def _plain(value: Any) -> Any:
    return iso(value) if isinstance(value, datetime) else value


def community_detail(s: Session, community_id: int) -> dict[str, Any] | None:
    community = s.get(Community, community_id)
    if community is None:
        return None
    data = {col.name: _plain(getattr(community, col.name))
            for col in Community.__table__.columns}
    host = community.host or ""

    watch = s.execute(select(
        WatchState.mode, WatchState.tier, WatchState.last_status, WatchState.last_detail,
        WatchState.consecutive_errors, WatchState.last_checked_at, WatchState.next_check_at,
        WatchState.last_new_at, WatchState.posts_seen,
    ).where(WatchState.community_id == community_id)).first()
    conn = s.execute(select(
        CircleConnection.state, CircleConnection.state_detail, CircleConnection.priority,
        CircleConnection.notes, CircleConnection.last_sync_at, CircleConnection.spaces_total,
        CircleConnection.spaces_readable,
    ).where(CircleConnection.host == host)).first() if host else None
    session_meta = s.execute(select(
        ReplaySession.source, ReplaySession.cookie_count, ReplaySession.member_label,
        ReplaySession.created_at, ReplaySession.encrypted_cookies.like("plain:%"),
    ).where(ReplaySession.host == host)).first() if host else None

    seg = segment_expr()
    leads = [{
        "id": lead.id, "created_at": iso(lead.created_at), "classification": lead.classification,
        "segment": segment, "not_sent_reason": reason if segment == "not_sent" else None,
        "job_title": lead.job_title, "vini_status": lead.vini_status,
        "vini_reason": lead.vini_reason, "post_url": url,
        "snippet": (content or "")[:200],
    } for lead, segment, reason, url, content in s.execute(
        select(Lead, seg, not_sent_reason_expr(), Post.url, Post.content)
        .join(Post, Lead.post_id == Post.id)
        .where(Post.community_id == community_id)
        .order_by(Lead.created_at.desc()).limit(50)
    ).all()]

    # activity_log.community is free text: the slug in most writers, the host
    # or its first label in a few. Match all three.
    names = {community.slug}
    if host:
        names.update({host, host.split(".")[0]})
    activity = [{
        "id": a.id, "created_at": iso(a.created_at), "kind": a.kind, "level": a.level,
        "summary": a.summary, "detail": a.detail or {},
    } for a in s.scalars(
        select(ActivityLog).where(ActivityLog.community.in_(names))
        .order_by(ActivityLog.id.desc()).limit(50)
    ).all()]
    fills = [{
        "created_at": iso(f.created_at), "label": f.label, "outcome": f.outcome,
        "account": f.account,
    } for f in s.scalars(
        select(JoinFormFill).where(JoinFormFill.community_id == community_id)
        .order_by(JoinFormFill.id.desc()).limit(20)
    ).all()]

    return {
        "community": data,
        "fit": bool(s.scalar(select(fit()).where(Community.id == community_id))),
        "watch": {key: _plain(value) for key, value in watch._mapping.items()} if watch else None,
        "conn": ({"state": conn.state, "bucket": connection_bucket(conn.state, conn.state_detail),
                  "detail": conn.state_detail, "priority": conn.priority, "notes": conn.notes,
                  "last_sync_at": iso(conn.last_sync_at), "spaces_total": conn.spaces_total,
                  "spaces_readable": conn.spaces_readable} if conn else None),
        "session": ({"source": session_meta[0], "cookie_count": session_meta[1],
                     "member_label": session_meta[2], "created_at": iso(session_meta[3]),
                     "plaintext": bool(session_meta[4])} if session_meta else None),
        "leads": leads,
        "activity": activity,
        "form_fills": fills,
    }


@router.get("/communities")
def api_communities(
    db=Depends(get_db), q: str | None = None, platform: str | None = None,
    icp: str | None = None, join_type: str | None = None, join_status: str | None = None,
    read_outcome: str | None = None, monitored: str | None = None,
    source: str | None = None, access: str | None = None,
    sort: str = "icp", dir: str = "desc",
    page: int = Query(1, ge=1), limit: int = Query(50, ge=1, le=MAX_LIMIT),
) -> dict[str, Any]:
    try:
        with db.session() as s:
            return query_communities(
                s, q=q, platform=platform, icp=icp, join_type=join_type,
                join_status=join_status, read_outcome=read_outcome, monitored=monitored,
                source=source, access=access, sort=sort, direction=dir, page=page,
                limit=limit,
            )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/communities/facets")
def api_community_facets(request: Request, db=Depends(get_db)) -> dict[str, Any]:
    def compute() -> dict[str, Any]:
        with db.session() as s:
            return community_facets(s)

    return cached(request, "community_facets", 300, compute)


@router.get("/communities/{community_id}")
def api_community(community_id: int, db=Depends(get_db)) -> dict[str, Any]:
    with db.session() as s:
        out = community_detail(s, community_id)
    if out is None:
        raise HTTPException(404, "Community not found")
    return out
