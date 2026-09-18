"""Client-facing overview: the whole funnel in one read-only payload.

Built for someone who has never seen the pipeline: every number is a plain
count straight from the database, each stage carries the timestamp of its most
recent change, and the split by discovery source shows where the volume (the
DNS host export) and the quality (Circle's own directory) actually come from.

Definitions -- kept here, in one place, because the page explains them to the
client verbatim:

* found     -- every community row we hold.
* alive     -- the community answered: it is a listing in Circle's own
               directory, or a request returned its name, or the join-type
               check got a definite answer. A bare 401 from ``*.circle.so`` does
               NOT count: Circle returns 401 for any subdomain, even invented
               ones (verified 2026-09-17), so ``locked_unknown`` alone proves
               nothing.
* named     -- we know the community's name (needed to judge ICP fit at all).
* icp       -- ICP-fit AND worth pursuing. Excluded by decision of 2026-09-18:
               communities not hosted on Circle, and communities whose owner's
               Circle subscription has lapsed (``subscription_expired``).
* read      -- ICP-fit communities whose posts we have read at least once.
* posts / leads -- everything read / found, per source of the community.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.orm import Session

from circle_leads.storage.models import (
    CircleConnection, Community, ConnectionState, JoinStatus, Lead, Post,
    ScanJob, Setting,
)

DNS_SOURCE = "file:combined_hosts_2026-09-15"

SOURCES = [
    ("directory", "Circle directory"),
    ("dns", "DNS host export"),
    ("other", "Other sources"),
]

# A join-type check that got a real answer from the community's own API.
DEFINITIVE_JOIN_TYPES = ("free_join", "paid", "invite_only", "subscription_expired")

# Platforms we can actually join and read. "discover" is a Circle directory
# card whose real host we have not resolved yet -- still on Circle.
CIRCLE_PLATFORMS = ("circle", "discover")

# Settings rows the worker writes after each scheduled stage.
ACTIVITY_SETTINGS = (
    "harvest_last_run", "harvest_schedule", "icp_classification_last_run",
    "enrichment_cursor_id",
)

CLOUDFLARE_MARKER = "cloudflare challenge"


def source_bucket():
    return case(
        (Community.discovery_source.like("%circle_directory"), "directory"),
        (Community.discovery_source == DNS_SOURCE, "dns"),
        else_="other",
    )


def connection_bucket(state: str | None, detail: str | None) -> str:
    """Group a cookie connection by what a human should do about it.

    The distinction that matters: a Cloudflare challenge on the cloud worker
    is NOT a dead cookie -- the same cookies read fine from a home IP -- so
    telling the user to refresh it sends them on a pointless errand.
    """
    if state == ConnectionState.CONNECTED.value:
        return "working"
    if CLOUDFLARE_MARKER in (detail or "").lower():
        return "cloudflare_blocked"
    if state == ConnectionState.SESSION_EXPIRED.value:
        return "session_expired"
    if state in (ConnectionState.ERROR.value, ConnectionState.ACCESS_DENIED.value):
        return "error"
    return "not_connected"


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _count(condition) -> Any:
    return func.coalesce(func.sum(case((condition, 1), else_=0)), 0)


def build_overview(s: Session, now: datetime) -> dict[str, Any]:
    """``now`` is naive UTC, like every timestamp in the database."""
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    src = source_bucket().label("src")

    named = and_(Community.name.is_not(None), Community.name != "")
    alive = or_(
        Community.discovery_source.like("%circle_directory"),
        named,
        Community.join_type.in_(DEFINITIVE_JOIN_TYPES),
    )
    on_circle = or_(Community.platform.is_(None), Community.platform.in_(CIRCLE_PLATFORMS))
    not_expired = or_(
        Community.join_type.is_(None), Community.join_type != "subscription_expired",
    )
    icp = and_(Community.icp_flag.is_(True), on_circle, not_expired)
    read = Community.last_synced_at.is_not(None)

    # --- 1. funnel by source ---------------------------------------------
    rows = s.execute(
        select(
            src,
            func.count(),
            _count(alive), _count(named), _count(icp), _count(and_(icp, read)),
            _count(and_(read, ~icp)),
            _count(Community.discovered_at >= today_start),
        ).group_by(src)
    ).all()
    per_source = {
        r[0]: {
            "found": int(r[1]), "alive": int(r[2]), "named": int(r[3]),
            "icp": int(r[4]), "read": int(r[5]), "read_other": int(r[6]),
            "found_today": int(r[7]),
        }
        for r in rows
    }
    post_src = source_bucket().label("src")
    for key, n in s.execute(
        select(post_src, func.count()).select_from(Post)
        .join(Community, Community.id == Post.community_id).group_by(post_src)
    ).all():
        per_source.setdefault(key, {})["posts"] = int(n)
    lead_src = source_bucket().label("src")
    for key, n in s.execute(
        select(lead_src, func.count()).select_from(Lead)
        .join(Post, Post.id == Lead.post_id)
        .join(Community, Community.id == Post.community_id)
        .where(Lead.classification == "LEAD").group_by(lead_src)
    ).all():
        per_source.setdefault(key, {})["leads"] = int(n)

    stages = ("found", "alive", "named", "icp", "read", "posts", "leads",
              "read_other", "found_today")
    sources = []
    for key, label in SOURCES:
        counts = per_source.get(key, {})
        sources.append({"key": key, "label": label,
                        **{st: counts.get(st, 0) for st in stages}})
    total = {st: sum(x[st] for x in sources) for st in stages}

    # One round trip: the database is an ocean away from the function region.
    u = s.execute(select(
        select(func.max(Community.discovered_at)).scalar_subquery(),
        select(func.max(Community.join_type_checked_at)).scalar_subquery(),
        # No dedicated "name found at" column; the row's last change is the
        # closest honest proxy and the page labels it as such.
        select(func.max(Community.updated_at)).where(named).scalar_subquery(),
        select(func.max(Community.icp_checked_at)).scalar_subquery(),
        select(func.max(Community.last_synced_at)).scalar_subquery(),
        select(func.max(Post.scraped_at)).scalar_subquery(),
        select(func.max(Lead.created_at)).where(Lead.classification == "LEAD")
        .scalar_subquery(),
    )).one()
    updated = dict(zip(("found", "alive", "named", "icp", "read", "posts", "leads"), u))

    # --- 2. ICP-fit by how reachable they are ------------------------------
    fit = Community.icp_flag.is_(True)
    jt, js = Community.join_type, Community.join_status
    free = and_(fit, on_circle, jt == "free_join")
    paid = and_(fit, on_circle, jt == "paid")
    closed = and_(fit, on_circle, or_(jt.is_(None), ~jt.in_(
        ("free_join", "paid", "subscription_expired"))))
    waiting = (JoinStatus.NOT_ATTEMPTED.value, JoinStatus.QUEUED.value,
               JoinStatus.PENDING_APPROVAL.value)
    g = s.execute(select(
        _count(and_(free, js == JoinStatus.JOINED.value)),
        _count(and_(free, js.in_(waiting))),
        _count(and_(free, js == JoinStatus.PAID_SKIP.value)),
        _count(and_(free, ~js.in_(waiting + (JoinStatus.JOINED.value,
                                             JoinStatus.PAID_SKIP.value)))),
        _count(and_(paid, Community.platform == "discover")),
        _count(and_(paid, or_(Community.platform.is_(None),
                              Community.platform != "discover"))),
        _count(and_(closed, jt == "locked_unknown")),
        _count(and_(closed, jt == "invite_only")),
        _count(and_(closed, or_(jt.is_(None), jt == "unknown"))),
        _count(and_(fit, ~on_circle)),
        _count(and_(fit, on_circle, jt == "subscription_expired")),
        _count(and_(fit, read)),
    )).one()
    g = [int(v) for v in g]
    icp_groups = {
        "free": {"joined": g[0], "waiting": g[1], "hit_paywall": g[2], "failed": g[3]},
        "paid": {"directory_cards": g[4], "on_circle": g[5]},
        "closed": {"locked": g[6], "invite_only": g[7], "unclear": g[8]},
        "excluded": {"not_on_circle": g[9], "subscription_expired": g[10]},
    }
    icp_groups["free"]["total"] = sum(icp_groups["free"].values())
    icp_groups["paid"]["total"] = sum(icp_groups["paid"].values())
    icp_groups["closed"]["total"] = sum(icp_groups["closed"].values())
    icp_groups["excluded"]["total"] = sum(icp_groups["excluded"].values())
    icp_groups["total"] = (icp_groups["free"]["total"] + icp_groups["paid"]["total"]
                           + icp_groups["closed"]["total"])

    # --- 3. cookie connections ----------------------------------------------
    connections = []
    for c in s.scalars(select(CircleConnection).order_by(CircleConnection.host)).all():
        connections.append({
            "host": c.host, "name": c.name or c.host,
            "bucket": connection_bucket(c.state, c.state_detail),
            "detail": c.state_detail,
            "spaces_readable": c.spaces_readable, "spaces_total": c.spaces_total,
            "last_sync_at": _iso(c.last_sync_at),
        })
    buckets: dict[str, int] = {}
    for c in connections:
        buckets[c["bucket"]] = buckets.get(c["bucket"], 0) + 1

    # --- 4. leads ------------------------------------------------------------
    is_lead = Lead.classification == "LEAD"
    by_community = [
        {"name": r[0] or r[1], "url": r[1], "leads": int(r[2]),
         "today": int(r[3]), "last": _iso(r[4])}
        for r in s.execute(
            select(Community.name, Community.url, func.count(),
                   _count(Lead.created_at >= today_start), func.max(Lead.created_at))
            .select_from(Lead).join(Post, Post.id == Lead.post_id)
            .join(Community, Community.id == Post.community_id)
            .where(is_lead).group_by(Community.id, Community.name, Community.url)
            .order_by(func.count().desc(), func.max(Lead.created_at).desc())
        ).all()
    ]
    recent = [
        {"id": r[0], "created_at": _iso(r[1]), "job_title": r[2], "company": r[3],
         "priority": r[4], "quote": (r[5] or "")[:280], "post_title": r[6],
         "post_url": r[7], "community": r[8] or r[9]}
        for r in s.execute(
            select(Lead.id, Lead.created_at, Lead.job_title, Lead.company,
                   Lead.priority, Lead.evidence_quote, Post.title, Post.url,
                   Community.name, Community.url)
            .select_from(Lead).join(Post, Post.id == Lead.post_id)
            .join(Community, Community.id == Post.community_id)
            .where(is_lead).order_by(Lead.created_at.desc()).limit(10)
        ).all()
    ]

    # --- 5. what is happening now -------------------------------------------
    settings = {
        k: {"value": v, "updated_at": _iso(u)}
        for k, v, u in s.execute(
            select(Setting.key, Setting.value, Setting.updated_at)
            .where(Setting.key.in_(ACTIVITY_SETTINGS))
        ).all()
    }
    unnamed = ~named
    a = s.execute(select(
        select(func.count()).select_from(Community).where(unnamed).scalar_subquery(),
        select(func.count()).select_from(Community)
        .where(unnamed, Community.discovery_source == DNS_SOURCE).scalar_subquery(),
        select(func.max(Community.updated_at))
        .where(Community.discovery_source == DNS_SOURCE).scalar_subquery(),
        select(func.count()).select_from(Post)
        .where(Post.scraped_at >= today_start).scalar_subquery(),
        select(func.count()).select_from(Community)
        .where(Community.last_synced_at >= today_start).scalar_subquery(),
        select(func.count()).select_from(ScanJob)
        .where(ScanJob.state.in_(("queued", "running"))).scalar_subquery(),
        select(func.count()).select_from(Lead).where(Lead.classification == "LEAD")
        .scalar_subquery(),
        select(func.count()).select_from(Lead)
        .where(Lead.classification == "LEAD", Lead.created_at >= today_start)
        .scalar_subquery(),
    )).one()
    activity = {
        "settings": settings,
        "unnamed_total": a[0] or 0,
        "unnamed_dns": a[1] or 0,
        "dns_last_change": _iso(a[2]),
        "posts_today": a[3] or 0,
        "read_today": a[4] or 0,
        "jobs_active": a[5] or 0,
    }
    leads_total, leads_today = a[6] or 0, a[7] or 0

    return {
        "generated_at": _iso(now),
        "today_start": _iso(today_start),
        "funnel": {"sources": sources, "total": total,
                   "updated": {k: _iso(v) for k, v in updated.items()}},
        "icp_groups": icp_groups,
        "connections": {"counts": buckets, "items": connections},
        "leads": {"total": leads_total, "today": leads_today,
                  "by_community": by_community, "recent": recent},
        "activity": activity,
    }
