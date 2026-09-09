"""Tests for DB-backed runtime settings and the harvest schedule."""

from datetime import datetime, timedelta

import pytest

from circle_leads.storage.database import Database
from circle_leads.storage.settings_store import (
    DEFAULT_SCHEDULE,
    get_schedule,
    is_harvest_due,
    mark_harvest_run,
    set_schedule,
    get_setting,
    set_setting,
)


@pytest.fixture
def db(tmp_path):
    return Database(f"sqlite:///{tmp_path}/settings.db")


def test_default_schedule(db):
    assert get_schedule(db) == DEFAULT_SCHEDULE


def test_set_and_get_schedule(db):
    set_schedule(db, "hourly")
    assert get_schedule(db) == "hourly"


def test_invalid_schedule_rejected(db):
    with pytest.raises(ValueError):
        set_schedule(db, "every_minute")


def test_off_schedule_never_due(db):
    set_schedule(db, "off")
    assert is_harvest_due(db) is False


def test_due_when_never_run(db):
    set_schedule(db, "hourly")
    assert is_harvest_due(db) is True


def test_not_due_right_after_run(db):
    set_schedule(db, "hourly")
    mark_harvest_run(db)
    assert is_harvest_due(db) is False


def test_due_again_after_interval(db):
    set_schedule(db, "hourly")
    mark_harvest_run(db)
    later = datetime.utcnow() + timedelta(hours=2)
    assert is_harvest_due(db, now=later) is True


def test_daily_not_due_after_a_few_hours(db):
    set_schedule(db, "daily")
    mark_harvest_run(db)
    soon = datetime.utcnow() + timedelta(hours=3)
    assert is_harvest_due(db, now=soon) is False


def test_generic_setting_roundtrip(db):
    set_setting(db, "foo", "bar")
    assert get_setting(db, "foo") == "bar"
    assert get_setting(db, "missing", "default") == "default"


def test_custom_schedule(db):
    set_schedule(db, "custom:3")
    assert get_schedule(db) == "custom:3"
    mark_harvest_run(db)
    soon = datetime.utcnow() + timedelta(hours=2)
    later = datetime.utcnow() + timedelta(hours=4)
    assert is_harvest_due(db, now=soon) is False    # < 3h
    assert is_harvest_due(db, now=later) is True     # > 3h


def test_invalid_custom_schedule_rejected(db):
    with pytest.raises(ValueError):
        set_schedule(db, "custom:abc")
    with pytest.raises(ValueError):
        set_schedule(db, "custom:0")


# --- Sub-hourly (near-real-time) schedules ---------------------------------

from circle_leads.storage.settings_store import _schedule_minutes, set_schedule


def test_schedule_minutes_named_and_custom():
    assert _schedule_minutes("every_5min") == 5
    assert _schedule_minutes("every_15min") == 15
    assert _schedule_minutes("hourly") == 60
    assert _schedule_minutes("daily") == 1440
    assert _schedule_minutes("off") is None
    assert _schedule_minutes("custom:5m") == 5
    assert _schedule_minutes("custom:90m") == 90
    assert _schedule_minutes("custom:2h") == 120
    assert _schedule_minutes("custom:3") == 180  # bare number = hours (back-compat)
    assert _schedule_minutes("custom:0m") is None
    assert _schedule_minutes("nonsense") is None


def test_five_minute_schedule_due_logic(tmp_path):
    from datetime import datetime, timedelta, timezone
    from circle_leads.storage.database import Database
    from circle_leads.storage.settings_store import (
        is_harvest_due, mark_harvest_run,
    )

    db = Database("sqlite:///" + str(tmp_path / "s.db"))
    set_schedule(db, "every_5min")
    assert is_harvest_due(db) is True  # never run

    mark_harvest_run(db)
    assert is_harvest_due(db) is False  # just ran

    # Due again after 5 minutes.
    now = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(minutes=6)
    assert is_harvest_due(db, now=now) is True


def test_set_schedule_accepts_minute_forms(tmp_path):
    from circle_leads.storage.database import Database
    db = Database("sqlite:///" + str(tmp_path / "s.db"))
    set_schedule(db, "every_5min")     # no raise
    set_schedule(db, "custom:10m")     # no raise
    import pytest
    with pytest.raises(ValueError):
        set_schedule(db, "custom:0m")


# --- effective requirements (YAML defaults + DB override) -----------------

def test_load_effective_requirements_returns_defaults_without_override(db):
    from circle_leads.storage.settings_store import load_effective_requirements

    req = load_effective_requirements(db)
    assert req.max_post_age_days == 365  # packaged/model default


def test_load_effective_requirements_applies_the_db_override(db):
    from circle_leads.storage.settings_store import (
        load_effective_requirements, set_requirements_override,
    )
    from circle_leads.config.settings import requirements_to_dict

    base = requirements_to_dict(load_effective_requirements(db))
    base["max_post_age_days"] = 90
    set_requirements_override(db, base)

    assert load_effective_requirements(db).max_post_age_days == 90


def test_load_effective_requirements_ignores_a_corrupt_override(db):
    from circle_leads.storage.settings_store import load_effective_requirements
    from circle_leads.storage.settings_store import REQUIREMENTS_KEY

    set_setting(db, REQUIREMENTS_KEY, "{not valid json")
    req = load_effective_requirements(db)
    assert req.max_post_age_days == 365  # fell back to defaults
