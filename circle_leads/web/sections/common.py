"""SQL expressions more than one section counts with.

One definition each, so a number on the analytics page and the list it links
to cannot disagree. Everything here runs on SQLite (the tests) and Postgres.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import and_, case, exists, func, or_, select

from circle_leads.reach import has_session, on_circle
from circle_leads.storage.models import Community, JoinStatus, Lead, Post, ReplaySession

# What the push stores in Lead.vini_status (circle_leads/export/vini_ingest.py).
VINI_STATUSES = ("accepted", "duplicate", "held", "rejected", "error")

# The segments the leads page splits on, in display order. "no_data": sent
# before Vini's answers were kept, so nobody knows what it said.
SEGMENTS = ("accepted", "duplicate", "held", "rejected", "error", "not_sent", "no_data")

# Why a lead never went to Vini, as a person should read it.
NOT_SENT_REASONS = ("not_lead", "duplicate_of", "held_for_review", "no_author", "pending")

# The triage path's words for a lead it kept back after an LLM outage
# (circle_leads/triage/pipeline.py).
HELD_FOR_REVIEW_MARK = "Held for review, not sent to Vini"


def count_if(condition) -> Any:
    """COUNT of rows matching ``condition``, portable to SQLite."""
    return func.coalesce(func.sum(case((condition, 1), else_=0)), 0)


def lead_live():
    """A lead as the funnel counts it: judged a lead, and not a copy of one."""
    return and_(Lead.classification == "LEAD", Lead.duplicate_of_id.is_(None))


def fit():
    """An ICP-fit community we could ever do something with: on Circle and not
    a community whose own Circle plan has lapsed.

    No "known address" condition: 273 of the paid ones are Circle directory
    cards without their own host, and they are paid communities all the same.
    """
    return and_(
        Community.icp_flag.is_(True),
        on_circle(),
        or_(Community.join_type.is_(None), Community.join_type != "subscription_expired"),
    )


def has_access():
    """We are inside: the bot joined, or a member session is stored."""
    return or_(Community.join_status == JoinStatus.JOINED.value, has_session())


def access_since(start):
    """Got inside since ``start``: joined then, or a session stored then."""
    return or_(
        Community.joined_at >= start,
        exists(select(ReplaySession.id).where(
            ReplaySession.host == Community.host, ReplaySession.created_at >= start)),
    )


def segment_expr():
    """Where a lead stands with Vini, as one word."""
    return case(
        (Lead.vini_status.in_(VINI_STATUSES), Lead.vini_status),
        (Lead.external_synced_at.is_not(None), "no_data"),
        else_="not_sent",
    )


def not_sent_reason_expr():
    """Our reason a lead did not go to Vini (needs ``posts`` in the query)."""
    return case(
        (Lead.classification != "LEAD", "not_lead"),
        (Lead.duplicate_of_id.is_not(None), "duplicate_of"),
        (func.coalesce(Lead.reason, "").like(f"%{HELD_FOR_REVIEW_MARK}%"), "held_for_review"),
        (Post.author_id.is_(None), "no_author"),
        else_="pending",
    )


def lead_counts_by_community(start):
    """Per community: live leads all-time, since ``start``, and the newest one."""
    return (
        select(
            Post.community_id.label("community_id"),
            func.count(Lead.id).label("leads_all"),
            count_if(Lead.created_at >= start).label("leads_today"),
            func.max(Lead.created_at).label("last_lead_at"),
        )
        .join(Lead, Lead.post_id == Post.id)
        .where(lead_live())
        .group_by(Post.community_id)
        .subquery()
    )
