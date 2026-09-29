"""What more than one dashboard section needs to say about a community.

* ``source_bucket`` -- where a row came from: Circle's own directory, the DNS
  host export, or anything else.
* ``connection_bucket`` -- what a person should do about a cookie connection.
  A Cloudflare challenge on the server is NOT a dead cookie: the same cookies
  read fine from a home IP, so "refresh it" would be a pointless errand.
* ``build_queues`` -- how many rows wait at each step and when the step last
  moved, i.e. when a row last *left* the queue (the timestamp its worker
  writes). A queue with rows waiting and no movement for
  ``STALE_QUEUE_HOURS`` is stale.

The client-facing overview that used to live here was replaced by the
dashboard's sections (circle_leads/web/sections).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import String, and_, case, cast, func, select
from sqlalchemy.orm import Session

from circle_leads.reach import join_queue, on_circle, readable, recheckable_unknown
from circle_leads.storage.models import Community, ConnectionState

DNS_SOURCE = "file:combined_hosts_2026-09-15"

# Set in icp_reasons by the 2026-09-18 clean-up: the host redirected to
# circle.so's marketing page, so its "name" was that page's title (erased).
# Such a row is dead, not waiting for a name.
DEAD_HOST_MARKER = "dead_host_marketing_title"

STALE_QUEUE_HOURS = 24

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
