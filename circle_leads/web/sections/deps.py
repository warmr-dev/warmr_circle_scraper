"""What every dashboard section needs: the database, the session check, the
clock, the viewer's "today", and a small per-instance cache.

Everything the sections share lives on ``app.state`` (set by ``create_app``),
not in module globals: a test builds a fresh app per test, and a module-level
cache would carry one test's numbers into the next.
"""

from __future__ import annotations

import hashlib
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from fastapi import HTTPException, Query, Request

from circle_leads.storage.database import Database
from circle_leads.web.auth import COOKIE_NAME


def get_db(request: Request) -> Database:
    return request.app.state.db


def require_session(request: Request) -> None:
    if not request.app.state.sessions.valid(request.cookies.get(COOKIE_NAME)):
        raise HTTPException(status_code=401, detail="Not authenticated")


def utc_now() -> datetime:
    """Naive UTC, like every timestamp in the database. A dependency, so a
    test can pin the clock with ``app.dependency_overrides``."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def tz_offset(tz: int = Query(0, ge=-840, le=840)) -> int:
    """The viewer's ``Date.getTimezoneOffset()``: minutes to ADD to local time
    to get UTC (UTC+7 is -420)."""
    return tz


def day_start_utc(now: datetime, tz: int) -> datetime:
    """Midnight of the viewer's current local day, as naive UTC.

    "Today" is the day the person looking at the page is living in. The old
    dashboard counted from UTC midnight, which for a viewer at UTC+7 starts
    "today" at 07:00 in the morning.
    """
    local = now - timedelta(minutes=tz)
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight + timedelta(minutes=tz)


def local_day_to_utc(value: str | None, tz: int, *, name: str) -> datetime | None:
    """A ``YYYY-MM-DD`` in the viewer's calendar, as the naive-UTC start of that day."""
    if not value:
        return None
    try:
        day = datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(400, f"{name} must be a date like 2026-09-21") from None
    return day + timedelta(minutes=tz)


def iso(value: datetime | None) -> str | None:
    """Naive UTC out as an explicit UTC string, so the browser does not read
    it as local time."""
    if value is None:
        return None
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value.isoformat(timespec="seconds") + "Z"


def fingerprint(*parts: Any) -> str:
    """A short stable hash of an item's state (see the attention list)."""
    raw = "\x1f".join("" if p is None else str(p) for p in parts)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def cached(request: Request, key: str, seconds: float, compute: Callable[[], Any]) -> Any:
    """Serve ``compute()`` from this instance's cache for ``seconds``."""
    cache: dict = request.app.state.cache
    hit = cache.get(key)
    now = time.monotonic()
    if hit is not None and now - hit[0] < seconds:
        return hit[1]
    value = compute()
    cache[key] = (now, value)
    return value
