"""Tell a person that a community login died, once, and never raise.

The worker and the watcher both notice a dead session. This is the one place
that turns that into the Telegram message, so the wording cannot drift and a
paused community stays quiet.
"""

from __future__ import annotations

import logging

from sqlalchemy import select

from circle_leads.notify.telegram import cookie_problem
from circle_leads.storage.models import CircleConnection, Community, ConnectionPriority

logger = logging.getLogger(__name__)

PROBLEM = "Куки не действуют. Нужно зайти и отправить сессию заново."


def report_cookie_death(db, host: str, *, community_id: int | None = None,
                        problem: str = PROBLEM) -> bool:
    """Send the login alert unless this community is paused. Never raises."""
    try:
        with db.session() as s:
            conn = s.scalar(select(CircleConnection).where(CircleConnection.host == host))
            if conn is not None and conn.priority == ConnectionPriority.PAUSED.value:
                return False
            community = s.get(Community, community_id) if community_id else None
            if community is None:
                community = s.scalar(select(Community).where(Community.host == host))
            label = ((community.name or "").strip() if community is not None else "") or host
        return cookie_problem(label, problem, host=host)
    except Exception:  # noqa: BLE001 - an alert must not stop a read
        logger.warning("could not report a dead session for %s", host, exc_info=True)
        return False
