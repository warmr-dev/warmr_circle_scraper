"""Section 5, second half: the activity log, filterable, newest first.

Paged by id rather than offset: new rows arrive every few seconds while
someone reads, and an offset would shift under them.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from circle_leads.storage.models import ActivityLog
from circle_leads.web.sections.deps import get_db, iso, require_session, utc_now

router = APIRouter(prefix="/api/dash", dependencies=[Depends(require_session)])

# The kinds the writers use (storage/models.py ActivityLog.kind), for the menu.
KINDS = ("read", "triage", "classify", "ingest", "export", "join", "discover",
         "harvest", "review")
LEVELS = ("info", "success", "warning", "error")


def query_activity(
    s: Session, *, now: datetime, kind: str | None = None, level: str | None = None,
    q: str | None = None, hours: int | None = None, before_id: int | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    a = ActivityLog
    conds = []
    if kind:
        conds.append(a.kind.in_(kind.split(",")))
    if level:
        conds.append(a.level.in_(level.split(",")))
    if q:
        like = f"%{q.strip()}%"
        conds.append(or_(a.community.ilike(like), a.summary.ilike(like)))
    if hours:
        conds.append(a.created_at >= now - timedelta(hours=hours))
    if before_id:
        conds.append(a.id < before_id)
    limit = max(1, min(limit, 200))
    rows = s.scalars(select(a).where(*conds).order_by(a.id.desc()).limit(limit)).all()
    return {
        "rows": [{
            "id": r.id, "created_at": iso(r.created_at), "kind": r.kind, "level": r.level,
            "community": r.community, "space": r.space, "summary": r.summary,
            "detail": r.detail or {}, "items_seen": r.items_seen,
            "leads_found": r.leads_found, "decided_by": r.decided_by,
        } for r in rows],
        "next_before_id": rows[-1].id if len(rows) == limit else None,
        "kinds": list(KINDS),
        "levels": list(LEVELS),
    }


@router.get("/activity")
def api_activity(
    db=Depends(get_db), now: datetime = Depends(utc_now), kind: str | None = None,
    level: str | None = None, q: str | None = None,
    hours: int | None = Query(None, ge=1, le=24 * 90),
    before_id: int | None = Query(None, ge=1), limit: int = Query(100, ge=1, le=200),
) -> dict[str, Any]:
    with db.session() as s:
        return query_activity(s, now=now, kind=kind, level=level, q=q, hours=hours,
                              before_id=before_id, limit=limit)
