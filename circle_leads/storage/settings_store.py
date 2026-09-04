"""Runtime settings stored in the DB, editable from the dashboard.

The harvest schedule lives here so it can be changed without a redeploy: the
worker reads it each run and decides whether enough time has passed.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from circle_leads.storage.database import Database
from circle_leads.storage.models import Setting, utcnow

# Named intervals the dashboard offers, in hours. "off" disables the schedule.
SCHEDULE_INTERVALS = {
    "off": None,
    "hourly": 1,
    "every_6h": 6,
    "every_12h": 12,
    "daily": 24,
    "twice_daily": 12,   # kept for label compatibility
    "weekly": 168,
}
DEFAULT_SCHEDULE = "twice_daily"

KEY_SCHEDULE = "harvest_schedule"
KEY_LAST_RUN = "harvest_last_run"


def get_setting(db: Database, key: str, default: str | None = None) -> str | None:
    with db.session() as s:
        row = s.get(Setting, key)
        return row.value if row is not None else default


def set_setting(db: Database, key: str, value: str | None) -> None:
    with db.session() as s:
        row = s.get(Setting, key)
        if row is None:
            s.add(Setting(key=key, value=value))
        else:
            row.value = value


def get_schedule(db: Database) -> str:
    return get_setting(db, KEY_SCHEDULE, DEFAULT_SCHEDULE) or DEFAULT_SCHEDULE


def _schedule_hours(schedule: str) -> int | None:
    """Hours for a schedule name, or a custom:<hours> value. None = off."""
    if schedule.startswith("custom:"):
        try:
            hours = int(schedule.split(":", 1)[1])
        except (ValueError, IndexError):
            return None
        return hours if hours > 0 else None
    return SCHEDULE_INTERVALS.get(schedule)


def set_schedule(db: Database, schedule: str) -> None:
    if schedule.startswith("custom:"):
        if _schedule_hours(schedule) is None:
            raise ValueError(f"Invalid custom schedule {schedule!r}; use custom:<hours>")
    elif schedule not in SCHEDULE_INTERVALS:
        raise ValueError(
            f"Unknown schedule {schedule!r}; choose from {sorted(SCHEDULE_INTERVALS)} or custom:<hours>"
        )
    set_setting(db, KEY_SCHEDULE, schedule)


def _parse_ts(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def is_harvest_due(db: Database, *, now: datetime | None = None) -> bool:
    """True when the configured interval has elapsed since the last harvest.

    The worker calls this: if the schedule is 'off' -> never; otherwise compares
    the interval against the recorded last-run time.
    """
    schedule = get_schedule(db)
    hours = _schedule_hours(schedule)
    if hours is None:
        return False  # 'off'
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    last = _parse_ts(get_setting(db, KEY_LAST_RUN))
    if last is None:
        return True  # never run
    return now - last >= timedelta(hours=hours)


def mark_harvest_run(db: Database, *, now: datetime | None = None) -> None:
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    set_setting(db, KEY_LAST_RUN, now.isoformat())
