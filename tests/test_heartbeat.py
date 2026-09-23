"""The liveness signal, and the false alarm that made it necessary.

The worker used to write its heartbeat at the top of its main loop. That point
is only reached between jobs, and one job can be a harvest that runs for hours,
so a perfectly healthy worker went silent and the watchdog reported it dead.
These tests pin the property that fixed it: the beat keeps arriving while the
caller is busy.
"""
from __future__ import annotations

import threading
import time

import pytest

from circle_leads.storage import heartbeat


class FakeDB:
    """Stands in for Database; records what was written."""

    def __init__(self, fail_times: int = 0):
        self.writes: list[tuple[str, str]] = []
        self.fail_times = fail_times
        self.lock = threading.Lock()


@pytest.fixture
def recorded(monkeypatch):
    db = FakeDB()

    def set_setting(_db, key, value):
        with db.lock:
            if db.fail_times > 0:
                db.fail_times -= 1
                raise RuntimeError("pool exhausted")
            db.writes.append((key, value))

    monkeypatch.setattr(heartbeat, "set_setting", set_setting)
    return db


def wait_for(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_the_first_beat_is_immediate(recorded):
    cancel = heartbeat.start_heartbeat(recorded, "worker_heartbeat", interval_s=60)
    try:
        assert wait_for(lambda: len(recorded.writes) >= 1)
        assert recorded.writes[0][0] == "worker_heartbeat"
    finally:
        cancel()


def test_it_keeps_beating_while_the_caller_is_busy(recorded):
    """The whole point: the beat does not depend on reaching a loop top."""
    cancel = heartbeat.start_heartbeat(recorded, "worker_heartbeat", interval_s=0.05)
    try:
        # Stand in for a long job: this thread does not cooperate at all.
        time.sleep(0.4)
        assert len(recorded.writes) >= 3
    finally:
        cancel()


def test_a_database_failure_does_not_stop_the_beat(recorded):
    recorded.fail_times = 2
    cancel = heartbeat.start_heartbeat(recorded, "worker_heartbeat", interval_s=0.05)
    try:
        assert wait_for(lambda: len(recorded.writes) >= 2, timeout=3.0)
    finally:
        cancel()


def test_cancel_stops_it(recorded):
    cancel = heartbeat.start_heartbeat(recorded, "worker_heartbeat", interval_s=0.05)
    assert wait_for(lambda: len(recorded.writes) >= 1)
    cancel()
    seen = len(recorded.writes)
    time.sleep(0.2)
    assert len(recorded.writes) == seen


def test_the_thread_never_holds_the_process_open(recorded):
    cancel = heartbeat.start_heartbeat(recorded, "worker_heartbeat", interval_s=60)
    try:
        threads = [t for t in threading.enumerate() if t.name == "heartbeat-worker_heartbeat"]
        assert threads and threads[0].daemon
    finally:
        cancel()


def test_the_value_written_is_a_readable_timestamp(recorded):
    from datetime import datetime

    cancel = heartbeat.start_heartbeat(recorded, "watcher_heartbeat", interval_s=60)
    try:
        assert wait_for(lambda: recorded.writes)
        # /api/watchdog parses this with fromisoformat; an unparseable stamp
        # reads as "unreadable" and alerts just as loudly as a dead service.
        datetime.fromisoformat(recorded.writes[0][1])
    finally:
        cancel()
