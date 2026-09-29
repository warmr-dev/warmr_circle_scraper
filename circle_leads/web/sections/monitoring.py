"""Section 2: the communities we check, how each check went, and the leads.

The list is the feed watcher's ``watch_state``: every community it polls (fast
every 2 minutes, slow every 15) and the ones it switched off, with the reason.
Leads are counted from ``leads`` itself, not from ``watch_state.leads_found``,
which only counts what the watcher found -- the harvest and the cookie scans
file leads for the same communities.

A stored session is described by its metadata only; the cookie blob is never
selected, so it cannot end up in a response by accident.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Request
from sqlalchemy import exists, select
from sqlalchemy.orm import Session

from circle_leads.storage.models import (
    CircleConnection, Community, JoinStatus, ReplaySession, WatchState,
)
from circle_leads.web.overview import connection_bucket
from circle_leads.web.sections.common import lead_counts_by_community
from circle_leads.web.sections.deps import (
    cached, day_start_utc, get_db, iso, require_session, tz_offset, utc_now,
)

router = APIRouter(prefix="/api/dash", dependencies=[Depends(require_session)])

# Answers that mean the check itself worked.
HEALTHY_STATUSES = ("ok", "not_modified")


def _sessions(s: Session) -> dict[str, dict[str, Any]]:
    rows = s.execute(select(
        ReplaySession.host, ReplaySession.source, ReplaySession.cookie_count,
        ReplaySession.member_label, ReplaySession.created_at, ReplaySession.updated_at,
        ReplaySession.encrypted_cookies.like("plain:%").label("plaintext"),
    )).all()
    return {
        r.host: {"source": r.source, "cookie_count": r.cookie_count,
                 "member_label": r.member_label, "created_at": iso(r.created_at),
                 "updated_at": iso(r.updated_at), "plaintext": bool(r.plaintext)}
        for r in rows
    }


def _connections(s: Session) -> dict[str, dict[str, Any]]:
    rows = s.execute(select(
        CircleConnection.host, CircleConnection.name, CircleConnection.state,
        CircleConnection.state_detail, CircleConnection.priority, CircleConnection.notes,
        CircleConnection.spaces_total, CircleConnection.spaces_readable,
        CircleConnection.last_sync_at, CircleConnection.community_id,
    )).all()
    return {
        r.host: {"name": r.name, "state": r.state,
                 "bucket": connection_bucket(r.state, r.state_detail),
                 "detail": r.state_detail, "priority": r.priority, "notes": r.notes,
                 "spaces_total": r.spaces_total, "spaces_readable": r.spaces_readable,
                 "last_sync_at": iso(r.last_sync_at), "community_id": r.community_id}
        for r in rows
    }


def build_monitoring(s: Session, now: datetime, start: datetime) -> dict[str, Any]:
    counts = lead_counts_by_community(start)
    session_stored = exists(select(ReplaySession.id).where(ReplaySession.host == WatchState.host))
    w = WatchState
    rows = s.execute(
        select(
            w.id, w.community_id, w.host, w.mode, w.tier, w.last_status, w.last_detail,
            w.consecutive_errors, w.last_checked_at, w.next_check_at, w.last_new_at,
            w.posts_seen, w.leads_found,
            Community.slug, Community.name, Community.url, Community.join_status,
            Community.join_type, Community.icp_flag,
            counts.c.leads_all, counts.c.leads_today, counts.c.last_lead_at,
            session_stored.label("has_session"),
        )
        .join(Community, Community.id == w.community_id)
        .outerjoin(counts, counts.c.community_id == w.community_id)
    ).all()
    sessions = _sessions(s)
    connections = _connections(s)

    out = []
    for r in rows:
        member = r.join_status == JoinStatus.JOINED.value or bool(r.has_session)
        out.append({
            "watch_id": r.id,
            "community_id": r.community_id,
            "slug": r.slug,
            "name": r.name or r.slug,
            "host": r.host,
            "url": r.url,
            "mode": r.mode,
            "tier": r.tier,
            "last_status": r.last_status,
            "last_detail": r.last_detail,
            "consecutive_errors": r.consecutive_errors or 0,
            "last_checked_at": iso(r.last_checked_at),
            "next_check_at": iso(r.next_check_at),
            "last_new_at": iso(r.last_new_at),
            "posts_seen": r.posts_seen or 0,
            "watcher_leads": r.leads_found or 0,
            "join_status": r.join_status,
            "join_type": r.join_type,
            "icp_flag": bool(r.icp_flag),
            "member": member,
            "has_session": bool(r.has_session),
            "leads_all": int(r.leads_all or 0),
            "leads_today": int(r.leads_today or 0),
            "last_lead_at": iso(r.last_lead_at),
            "conn": connections.get(r.host),
            "session": sessions.get(r.host),
        })
    out.sort(key=lambda x: (-x["leads_all"], x["mode"] == "off", x["name"].lower()))

    watched_hosts = {r["host"] for r in out}
    unwatched = [
        {"host": host, "conn": conn, "session": sessions.get(host)}
        for host, conn in sorted(connections.items()) if host not in watched_hosts
    ]

    active = [r for r in out if r["mode"] != "off"]
    failing = [r for r in active
               if r["last_status"] not in HEALTHY_STATUSES or r["consecutive_errors"] > 0]
    return {
        "generated_at": iso(now),
        "today_start": iso(start),
        "summary": {
            "total": len(out),
            "active": len(active),
            "off": len(out) - len(active),
            "fast": sum(1 for r in active if r["tier"] == "fast"),
            "slow": sum(1 for r in active if r["tier"] != "fast"),
            "with_session": sum(1 for r in active if r["mode"] == "cookie"),
            "failing": len(failing),
            "leads_today": sum(r["leads_today"] for r in out),
            "communities_with_leads_today": sum(1 for r in out if r["leads_today"]),
        },
        "rows": out,
        "unwatched_connections": unwatched,
    }


@router.get("/monitoring")
def api_monitoring(request: Request, db=Depends(get_db), now: datetime = Depends(utc_now),
                   tz: int = Depends(tz_offset)) -> dict[str, Any]:
    start = day_start_utc(now, tz)

    def compute() -> dict[str, Any]:
        with db.session() as s:
            return build_monitoring(s, now, start)

    return cached(request, f"monitoring:{start.isoformat()}", 30, compute)
