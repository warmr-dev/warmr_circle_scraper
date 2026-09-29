"""The viewer's "today" and the time format every section sends.

The old dashboard started "today" at UTC midnight: for a viewer at UTC+7
that is 07:00 in the morning, so a lead found at 06:30 local time belonged to
"yesterday" on the page and to "today" on the clock on the wall.
"""

from datetime import datetime

import pytest
from fastapi import HTTPException

from circle_leads.web.sections.deps import day_start_utc, iso, local_day_to_utc


@pytest.mark.parametrize("now, tz, start", [
    # UTC+7 (getTimezoneOffset() == -420)
    (datetime(2026, 9, 29, 11, 0), -420, datetime(2026, 9, 28, 17, 0)),
    (datetime(2026, 9, 28, 16, 59), -420, datetime(2026, 9, 27, 17, 0)),
    (datetime(2026, 9, 28, 17, 0), -420, datetime(2026, 9, 28, 17, 0)),
    # UTC
    (datetime(2026, 9, 29, 0, 30), 0, datetime(2026, 9, 29, 0, 0)),
    # UTC-5 (300): 03:00 UTC is still the previous local day
    (datetime(2026, 9, 29, 3, 0), 300, datetime(2026, 9, 28, 5, 0)),
    # UTC+5:30 (-330)
    (datetime(2026, 9, 29, 19, 0), -330, datetime(2026, 9, 29, 18, 30)),
])
def test_today_starts_at_the_viewers_midnight(now, tz, start):
    assert day_start_utc(now, tz) == start


def test_a_date_filter_means_the_viewers_calendar_day():
    assert local_day_to_utc("2026-09-29", -420, name="since") == datetime(2026, 9, 28, 17, 0)
    assert local_day_to_utc(None, -420, name="since") is None
    with pytest.raises(HTTPException):
        local_day_to_utc("29.09.2026", 0, name="since")


def test_times_leave_as_explicit_utc():
    """A naive stamp read by the browser as local time would be 7 hours off."""
    assert iso(datetime(2026, 9, 29, 11, 0, 5, 123)) == "2026-09-29T11:00:05Z"
    assert iso(None) is None


def test_an_out_of_range_offset_is_refused(tmp_path, monkeypatch):
    from tests.dash_fixtures import make_app

    client, _db, _app = make_app(tmp_path, monkeypatch)
    assert client.get("/api/dash/analytics", params={"tz": 900}).status_code == 422
    assert client.get("/api/dash/analytics", params={"tz": -420}).status_code == 200
