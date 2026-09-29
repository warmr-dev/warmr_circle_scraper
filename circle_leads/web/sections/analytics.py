"""Section 1: the funnel, all-time and today.

Found -> ICP-fit -> paid / free / closed -> inside, and the leads: found ->
sent to Vini -> what Vini said. The fit parts add up to the fit total by
construction (paid + free + other), so no number on the page can drift from
its neighbours.

"Today" per step:
- found: discovered today;
- fit (and its paid/free/other split): judged a fit today -- a flagged row is
  not re-stamped by a rescore, so icp_checked_at is when it became a fit;
- inside: joined today, or a session stored today;
- leads: the leads filed today, and what Vini said about those same leads.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Request
from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from circle_leads.reach import has_session, on_circle, real_host
from circle_leads.storage.models import Community, JoinStatus, Lead
from circle_leads.web.sections.common import (
    SEGMENTS, access_since, count_if, fit, has_access, lead_live, segment_expr,
)
from circle_leads.web.sections.deps import (
    cached, day_start_utc, get_db, iso, require_session, tz_offset, utc_now,
)

router = APIRouter(prefix="/api/dash", dependencies=[Depends(require_session)])


def build_analytics(s: Session, now: datetime, start: datetime) -> dict[str, Any]:
    """``now`` and ``start`` are naive UTC; ``start`` is the viewer's midnight."""
    c = Community
    is_fit = fit()
    fit_today = and_(is_fit, c.icp_checked_at >= start)
    paid = c.join_type == "paid"
    free = c.join_type == "free_join"
    other = or_(c.join_type.is_(None), c.join_type.notin_(("paid", "free_join")))
    on_circle_at_address = and_(on_circle(), real_host())
    inside = has_access()
    inside_today = and_(inside, access_since(start))

    row = s.execute(select(
        func.count(c.id),
        count_if(c.discovered_at >= start),
        count_if(on_circle_at_address),
        count_if(and_(on_circle_at_address, c.discovered_at >= start)),
        count_if(is_fit),
        count_if(fit_today),
        count_if(and_(is_fit, paid)),
        count_if(and_(fit_today, paid)),
        count_if(and_(is_fit, paid, has_session())),
        count_if(and_(is_fit, free)),
        count_if(and_(fit_today, free)),
        count_if(and_(is_fit, free, ~real_host())),
        count_if(and_(is_fit, other)),
        count_if(and_(fit_today, other)),
        count_if(inside),
        count_if(inside_today),
        count_if(and_(inside, is_fit)),
        count_if(and_(inside_today, is_fit)),
        count_if(c.join_status == JoinStatus.JOINED.value),
        count_if(has_session()),
    )).one()
    (found, found_today, circle, circle_today, fit_all, fit_t, paid_all, paid_t,
     paid_session, free_all, free_t, free_no_address, other_all, other_t,
     inside_all, inside_t, inside_fit, inside_fit_t, joined, with_session) = (
        int(v or 0) for v in row)

    seg = segment_expr()
    segments = {name: {"all": 0, "today": 0} for name in SEGMENTS}
    for name, total, today in s.execute(
        select(seg, func.count(Lead.id), count_if(Lead.created_at >= start))
        .where(lead_live())
        .group_by(seg)
    ).all():
        segments[name] = {"all": int(total or 0), "today": int(today or 0)}
    leads_all = sum(v["all"] for v in segments.values())
    leads_today = sum(v["today"] for v in segments.values())

    return {
        "generated_at": iso(now),
        "today_start": iso(start),
        "communities": {
            "found": {"all": found, "today": found_today,
                      "circle_all": circle, "circle_today": circle_today},
            "fit": {"all": fit_all, "today": fit_t},
            "paid": {"all": paid_all, "today": paid_t, "with_session": paid_session},
            "free": {"all": free_all, "today": free_t, "no_address": free_no_address},
            "other": {"all": other_all, "today": other_t},
            "access": {"all": inside_all, "today": inside_t,
                       "fit_all": inside_fit, "fit_today": inside_fit_t,
                       "joined": joined, "with_session": with_session},
        },
        "leads": {
            "found": {"all": leads_all, "today": leads_today},
            # Sent = Vini got it, whatever it answered: everything but not_sent.
            "sent": {"all": leads_all - segments["not_sent"]["all"],
                     "today": leads_today - segments["not_sent"]["today"]},
            "segments": segments,
        },
    }


@router.get("/analytics")
def api_analytics(request: Request, db=Depends(get_db), now: datetime = Depends(utc_now),
                  tz: int = Depends(tz_offset)) -> dict[str, Any]:
    start = day_start_utc(now, tz)

    def compute() -> dict[str, Any]:
        with db.session() as s:
            return build_analytics(s, now, start)

    # The funnel moves at the pace of the worker's slowest stage; a minute of
    # staleness costs nothing and saves a round of aggregates per page view.
    return cached(request, f"analytics:{start.isoformat()}", 60, compute)
