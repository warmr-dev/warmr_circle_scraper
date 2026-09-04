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
