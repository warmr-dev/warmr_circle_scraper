"""Recording what the system did, for the dashboard's activity view.

Deliberately narrow: this records *what was examined and what was concluded*,
never credentials and never a member dossier. Detail payloads are truncated so
a runaway value cannot bloat the database.
"""

from __future__ import annotations

import logging
from typing import Any

from circle_leads.storage.models import ActivityLog

logger = logging.getLogger(__name__)

MAX_SUMMARY_CHARS = 500
MAX_DETAIL_CHARS = 2000

# Never let a credential reach the log, whatever a caller passes.
_FORBIDDEN_DETAIL_KEYS = {
    "token", "access_token", "refresh_token", "authorization", "cookie",
    "api_key", "apikey", "password", "secret",
}


def _safe_detail(detail: dict | None) -> dict:
    if not detail:
        return {}
    out: dict[str, Any] = {}
    for k, v in detail.items():
        if k.lower() in _FORBIDDEN_DETAIL_KEYS:
            continue
        text = str(v)
        out[k] = text[:MAX_DETAIL_CHARS] if len(text) > MAX_DETAIL_CHARS else v
    return out


def log_activity(
    session,
    *,
    kind: str,
    summary: str,
    level: str = "info",
    community: str | None = None,
    space: str | None = None,
    detail: dict | None = None,
    items_seen: int = 0,
    leads_found: int = 0,
    decided_by: str | None = None,
) -> ActivityLog:
    """Append one activity record. Never raises into the caller's flow."""
    entry = ActivityLog(
        kind=kind,
        level=level,
        community=community,
        space=space,
        summary=(summary or "")[:MAX_SUMMARY_CHARS],
        detail=_safe_detail(detail),
        items_seen=items_seen,
        leads_found=leads_found,
        decided_by=decided_by,
    )
    session.add(entry)
    return entry


def recent_activity(session, *, limit: int = 100, kind: str | None = None) -> list[dict]:
    """Read the activity feed, newest first."""
    from sqlalchemy import select

    stmt = select(ActivityLog).order_by(ActivityLog.created_at.desc()).limit(limit)
    if kind:
        stmt = stmt.where(ActivityLog.kind == kind)

    return [
        {
            "id": a.id,
            "created_at": a.created_at.isoformat() if a.created_at else None,
            "kind": a.kind,
            "level": a.level,
            "community": a.community,
            "space": a.space,
            "summary": a.summary,
            "detail": a.detail or {},
            "items_seen": a.items_seen,
            "leads_found": a.leads_found,
            "decided_by": a.decided_by,
        }
        for a in session.scalars(stmt).all()
    ]
