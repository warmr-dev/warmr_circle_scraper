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
    """Hours for a schedule name, or a custom:<hours> value. None = off.

    Kept for callers that think in whole hours; sub-hour schedules round up to
    1 here. Use ``_schedule_minutes`` for the true (minute-granular) interval.
    """
    minutes = _schedule_minutes(schedule)
    if minutes is None:
        return None
    return max(1, round(minutes / 60))


def _schedule_minutes(schedule: str) -> int | None:
    """Interval in minutes, or None for 'off'.

    Accepts the named presets (in hours), ``custom:<hours>``, and a minute form
    ``custom:<n>m`` / ``every_5min`` for near-real-time polling.
    """
    if schedule in ("every_5min", "every_5m"):
        return 5
    if schedule in ("every_15min", "every_15m"):
        return 15
    if schedule.startswith("custom:"):
        raw = schedule.split(":", 1)[1].strip().lower()
        try:
            if raw.endswith("m"):
                minutes = int(raw[:-1])
            elif raw.endswith("h"):
                minutes = int(raw[:-1]) * 60
            else:
                minutes = int(raw) * 60  # bare number = hours (back-compat)
        except (ValueError, IndexError):
            return None
        return minutes if minutes > 0 else None
    hours = SCHEDULE_INTERVALS.get(schedule)
    return hours * 60 if hours is not None else None


_NAMED_SCHEDULES = set(SCHEDULE_INTERVALS) | {"every_5min", "every_15min"}


def set_schedule(db: Database, schedule: str) -> None:
    if schedule.startswith("custom:"):
        if _schedule_minutes(schedule) is None:
            raise ValueError(
                f"Invalid custom schedule {schedule!r}; use custom:<hours> or "
                f"custom:<n>m (e.g. custom:5m)"
            )
    elif schedule not in _NAMED_SCHEDULES:
        raise ValueError(
            f"Unknown schedule {schedule!r}; choose from {sorted(_NAMED_SCHEDULES)} "
            f"or custom:<hours> / custom:<n>m"
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
    minutes = _schedule_minutes(schedule)
    if minutes is None:
        return False  # 'off'
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    last = _parse_ts(get_setting(db, KEY_LAST_RUN))
    if last is None:
        return True  # never run
    return now - last >= timedelta(minutes=minutes)


def mark_harvest_run(db: Database, *, now: datetime | None = None) -> None:
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    set_setting(db, KEY_LAST_RUN, now.isoformat())
