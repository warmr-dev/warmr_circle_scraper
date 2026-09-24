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
* live      -- named AND the join-type check got a definite answer
               (``LIVE_JOIN_TYPES``). Named alone overstates it: names were
               looked up after the join-type pass, so thousands of named rows
               still carry ``unknown``, and some are lapsed subscriptions.
* unchecked -- named, but the join type is still ``unknown``: not re-checked
               since the name was found. Shown next to live, not in the funnel.
* icp       -- ICP-fit AND worth pursuing. Excluded by decision of 2026-09-18:
               communities not hosted on Circle, and communities whose owner's
               Circle subscription has lapsed (``subscription_expired``); by
               decision of 2026-09-24, paid communities we hold no login for
               (listed in section 2, counted nowhere else -- reach.py).
* read      -- ICP-fit communities whose posts we have read at least once.
* posts / leads -- everything read / found, per source of the community.

Queues (``QUEUES``) say how many rows wait at each step and when the step last
moved, i.e. when a row last *left* the queue -- the timestamp its worker writes.
A queue with rows waiting and no movement for ``STALE_QUEUE_HOURS`` is stale.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import String, and_, case, cast, func, or_, select
from sqlalchemy.orm import Session

from circle_leads.reach import (  # noqa: F401 - CIRCLE_PLATFORMS is re-exported
    CIRCLE_PLATFORMS, has_session, is_paid, join_queue, on_circle, pursued, readable,
    real_host, recheckable_unknown,
)
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

# A named community with one of these join types is proven to still be up.
# locked_unknown counts here though not in `alive`: with a name already in
# hand, the 401 is a real login wall rather than Circle's catch-all 401.
LIVE_JOIN_TYPES = ("free_join", "paid", "invite_only", "locked_unknown")

# Set in icp_reasons by the 2026-09-18 clean-up: the host redirected to
# circle.so's marketing page, so its "name" was that page's title (erased).
# Such a row is dead, not waiting for a name.
DEAD_HOST_MARKER = "dead_host_marketing_title"

STALE_QUEUE_HOURS = 24

# Settings rows the worker writes after each scheduled stage.
ACTIVITY_SETTINGS = (
    "harvest_last_run", "harvest_last_finish", "harvest_schedule",
    "icp_classification_last_run", "join_type_last_run", "enrichment_cursor_id",
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


def build_queues(s: Session, now: datetime, *, named) -> list[dict[str, Any]]:
    """How many rows wait at each step, and when that step last moved.

    "Moved" means a row left the queue, so each queue reads the timestamp its
    own worker writes -- over the rows that can leave it, not only those still
    waiting. Arrivals (new ICP-fit rows, say) are not movement: a queue can be
    fed all day and still be stuck.
    """
    c, jt = Community, Community.join_type
    fit = c.icp_flag.is_(True)
    dead = func.coalesce(cast(c.icp_reasons, String), "").like(f"%{DEAD_HOST_MARKER}%")
    # Every queue counts exactly the rows its worker will take -- the rules live
    # in circle_leads/reach.py and the workers use the same ones. A queue that
    # counts rows nothing will ever take is red forever, and a warning that
    # cannot clear teaches the reader to ignore the colour: the join queue once
    # said 87 for 21 joinable communities, and a "decision on payment" queue
    # counted 301 paid communities that no process decides.
    specs = [
        # No "name found at" column. The harvest and the join bot touch named
        # rows every few minutes, which would make a stalled name lookup look
        # busy, so their rows are left out of the proxy.
        ("name", and_(~named, ~dead), c.updated_at,
         and_(named, c.last_synced_at.is_(None), c.join_attempted_at.is_(None))),
        # Only an "unknown" another check can still change. A host that left
        # Circle, or never was on it, is not waiting for anything: 27 of the
        # 443 once counted here redirect to circle.so's own site, 20 answer
        # 404, 13 are ordinary websites.
        ("join_type_recheck", and_(named, on_circle(), recheckable_unknown()),
         c.join_type_checked_at, and_(named, on_circle())),
        # Readable and never read -- read with a session or anonymously. A
        # paid community without a login is not waiting to be read: nothing
        # reads it, by decision (reach.py).
        ("read", and_(fit, readable(), c.last_synced_at.is_(None)),
         c.last_synced_at, and_(fit, readable())),
        ("join", join_queue(),
         c.join_attempted_at, and_(fit, on_circle(), jt == "free_join")),
    ]
    # One round trip, as elsewhere on this page.
    values = s.execute(select(*[
        sub
        for _, waiting, field, moved_in in specs
        for sub in (
            select(func.count()).select_from(c).where(waiting).scalar_subquery(),
            select(func.max(field)).where(moved_in).scalar_subquery(),
        )
    ])).one()
    stale_after = timedelta(hours=STALE_QUEUE_HOURS)
    queues = []
    for i, (key, _, field, _) in enumerate(specs):
        waiting, moved = int(values[2 * i] or 0), values[2 * i + 1]
        queues.append({
            "key": key, "waiting": waiting, "last_moved": _iso(moved),
            "field": field.key,
            "stale": bool(waiting) and (moved is None or now - moved > stale_after),
        })
    return queues


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
    circle = on_circle()
    not_expired = or_(
        Community.join_type.is_(None), Community.join_type != "subscription_expired",
    )
    # A paid community we hold no login for is not pursued: counted and listed
    # in section 2, and nowhere else (reach.py).
    icp = and_(Community.icp_flag.is_(True), circle, not_expired, pursued())
    read = Community.last_synced_at.is_not(None)
    jt = Community.join_type
    live = and_(named, jt.in_(LIVE_JOIN_TYPES))

    # --- 1. funnel by source ---------------------------------------------
    rows = s.execute(
        select(
            src,
            func.count(),
            _count(alive), _count(named), _count(icp), _count(and_(icp, read)),
            _count(and_(read, ~icp)),
            _count(Community.discovered_at >= today_start),
            _count(live), _count(and_(named, jt == "unknown")),
            _count(and_(named, jt == "subscription_expired")),
            _count(and_(named, jt.is_(None))),
        ).group_by(src)
    ).all()
    per_source = {
        r[0]: {
            "found": int(r[1]), "alive": int(r[2]), "named": int(r[3]),
            "icp": int(r[4]), "read": int(r[5]), "read_other": int(r[6]),
            "found_today": int(r[7]), "live": int(r[8]),
            # The rest of `named`, so the split adds up on the page.
            "unchecked": int(r[9]), "named_lapsed": int(r[10]),
            "named_untyped": int(r[11]),
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

    stages = ("found", "alive", "named", "live", "icp", "read", "posts", "leads",
              "read_other", "found_today", "unchecked", "named_lapsed",
              "named_untyped")
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
        select(func.max(Community.join_type_checked_at)).where(live).scalar_subquery(),
        select(func.max(Community.icp_checked_at)).scalar_subquery(),
        select(func.max(Community.last_synced_at)).scalar_subquery(),
        select(func.max(Post.scraped_at)).scalar_subquery(),
        select(func.max(Lead.created_at)).where(Lead.classification == "LEAD")
        .scalar_subquery(),
    )).one()
    updated = dict(zip(
        ("found", "alive", "named", "live", "icp", "read", "posts", "leads"), u))

    # --- 2. ICP-fit by how reachable they are ------------------------------
    fit = Community.icp_flag.is_(True)
    js = Community.join_status
    session = has_session()
    free = and_(fit, circle, jt == "free_join")
    paid = and_(fit, circle, is_paid())
    closed = and_(fit, circle, or_(jt.is_(None), ~jt.in_(
        ("free_join", "paid", "subscription_expired"))))
    # Each free community lands in exactly one bucket, first match wins, so the
    # buckets add up and "waiting" is the join queue's own number: a queue row
    # never has a session unless it sits on the profile step, and that step is
    # checked before "joined".
    free_bucket = case(
        (or_(js == JoinStatus.JOINED.value,
             and_(session, js != JoinStatus.PROFILE_PENDING.value)), "joined"),
        (join_queue(), "waiting"),
        (js == JoinStatus.PENDING_APPROVAL.value, "pending_approval"),
        (js == JoinStatus.PAID_SKIP.value, "hit_paywall"),
        (~real_host(), "no_address"),
        else_="failed",
    ).label("bucket")
    free_counts = dict.fromkeys(
        ("joined", "waiting", "pending_approval", "hit_paywall", "no_address", "failed"), 0)
    for bucket, n in s.execute(
        select(free_bucket, func.count()).where(free).group_by(free_bucket)
    ).all():
        free_counts[bucket] = int(n)
    g = s.execute(select(
        _count(and_(paid, session)),
        _count(and_(paid, ~session)),
        _count(and_(closed, jt == "locked_unknown")),
        _count(and_(closed, jt == "invite_only")),
        _count(and_(closed, or_(jt.is_(None), jt == "unknown"))),
        _count(and_(fit, ~circle)),
        _count(and_(fit, circle, jt == "subscription_expired")),
    )).one()
    g = [int(v) for v in g]
    # Paid is a plain list, by decision of 2026-09-24: never joined, read only
    # with a login someone bought, and outside every other count on the page.
    paid_items = [
        {"name": r.name or r.host or r.url, "url": r.url, "price": r.price_label,
         "has_session": bool(r.has_session)}
        for r in s.execute(
            select(Community.name, Community.url, Community.host,
                   Community.price_label, session.label("has_session"))
            .where(paid).order_by(func.lower(func.coalesce(Community.name, Community.url)))
        ).all()
    ]
    icp_groups = {
        "free": {**free_counts, "total": sum(free_counts.values())},
        "paid": {"with_session": g[0], "without_session": g[1], "total": g[0] + g[1],
                 "items": paid_items},
        "closed": {"locked": g[2], "invite_only": g[3], "unclear": g[4]},
        "excluded": {"not_on_circle": g[5], "subscription_expired": g[6]},
    }
    icp_groups["closed"]["total"] = sum(icp_groups["closed"].values())
    icp_groups["excluded"]["total"] = sum(icp_groups["excluded"].values())
    # What we can pursue: paid ones only where a login makes them readable.
    icp_groups["total"] = (icp_groups["free"]["total"] + icp_groups["closed"]["total"]
                           + icp_groups["paid"]["with_session"])

    queues = build_queues(s, now, named=named)

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
        "queues": queues,
        "connections": {"counts": buckets, "items": connections},
        "leads": {"total": leads_total, "today": leads_today,
                  "by_community": by_community, "recent": recent},
        "activity": activity,
    }
