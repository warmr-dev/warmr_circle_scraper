"""Rate limiting for the auto-join batch, from ``Requirements.join_pacing``.

One account joining dozens of communities a day should look like a person
exploring communities, not a script -- see
``circle_leads.config.settings.JoinPacingConfig``.
"""

from __future__ import annotations

import random
import time
from datetime import datetime

from sqlalchemy import func, select

from circle_leads.config.settings import JoinPacingConfig
from circle_leads.storage.database import Database
from circle_leads.storage.models import Community


def joins_attempted_today(db: Database) -> int:
    """Count of communities with a *persisted* outcome since UTC midnight.

    Only half of the daily-cap picture, and the conservative half: a handoff
    never reaches the DB, so this cannot see a community the browser opened
    without reaching a terminal outcome. ``joiner._attempts_today`` combines it
    with the attempt log, which counts every visit -- the quantity Circle
    actually reacts to.
    """
    start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    with db.session() as s:
        return (
            s.scalar(select(func.count(Community.id)).where(Community.join_attempted_at >= start))
            or 0
        )


def sleep_between_attempts(pacing: JoinPacingConfig) -> None:
    """Sleep min_delay_seconds..min_delay_seconds+jitter_seconds before the next attempt."""
    delay = pacing.min_delay_seconds + random.uniform(0, pacing.jitter_seconds)
    time.sleep(delay)
