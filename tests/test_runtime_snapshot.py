"""What a service tells the dashboard about itself, and what it must not.

The dashboard runs on Vercel and reads only the database, so the worker and
the watcher write a snapshot next to their heartbeat: host, code, model, which
keys are set, and the stage they are in. Two properties matter more than the
rest: the heartbeat itself is never delayed or broken by the snapshot, and no
key's value ever leaves the process -- only whether it is set.
"""
from __future__ import annotations

import json
import threading
import time

import pytest

from circle_leads.storage import heartbeat, runtime


class FakeDB:
    def __init__(self):
        self.writes: list[tuple[str, str]] = []
        self.lock = threading.Lock()


@pytest.fixture
def recorded(monkeypatch):
    db = FakeDB()

    def set_setting(_db, key, value):
        with db.lock:
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


def test_the_heartbeat_is_written_before_the_snapshot(recorded):
    cancel = heartbeat.start_heartbeat(recorded, "worker_heartbeat", interval_s=60,
                                       runtime_key="worker_runtime", service="worker")
    try:
        assert wait_for(lambda: len(recorded.writes) >= 2)
        assert [k for k, _ in recorded.writes[:2]] == ["worker_heartbeat", "worker_runtime"]
        snap = json.loads(recorded.writes[1][1])
        assert snap["service"] == "worker"
        assert snap["host"] and snap["pid"] and snap["beat_at"]
    finally:
        cancel()


def test_only_whether_a_key_is_set_leaves_the_process(recorded, monkeypatch):
    monkeypatch.setenv("VINI_API_SECRET", "vini-secret-value-123")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-not-a-real-key-456")
    monkeypatch.delenv("EXA_API_KEY", raising=False)
    cancel = heartbeat.start_heartbeat(recorded, "worker_heartbeat", interval_s=60,
                                       runtime_key="worker_runtime", service="worker")
    try:
        assert wait_for(lambda: len(recorded.writes) >= 2)
        raw = recorded.writes[1][1]
        assert "vini-secret-value-123" not in raw
        assert "sk-not-a-real-key-456" not in raw
        env = json.loads(raw)["env"]
        assert env["VINI_API_SECRET"] is True
        assert env["EXA_API_KEY"] is False
        assert all(isinstance(v, bool) for v in env.values())
    finally:
        cancel()


def test_a_broken_snapshot_does_not_stop_the_beat(recorded, monkeypatch):
    def broken(_static):
        raise RuntimeError("governor file unreadable")

    monkeypatch.setattr(runtime, "snapshot", broken)
    cancel = heartbeat.start_heartbeat(recorded, "watcher_heartbeat", interval_s=0.05,
                                       runtime_key="watcher_runtime", service="watcher")
    try:
        assert wait_for(lambda: sum(k == "watcher_heartbeat" for k, _ in recorded.writes) >= 3)
        assert all(k == "watcher_heartbeat" for k, _ in recorded.writes)
    finally:
        cancel()


def test_the_current_stage_is_in_the_snapshot():
    static = runtime.static_snapshot("worker")
    assert runtime.snapshot(static)["stage"]["stage"] is None
    with runtime.stage("harvest"):
        inside = runtime.snapshot(static)["stage"]
        with runtime.stage("icp_classification"):
            assert runtime.snapshot(static)["stage"]["stage"] == "icp_classification"
        assert runtime.snapshot(static)["stage"]["stage"] == "harvest"
    assert inside["stage"] == "harvest" and inside["since"]
    assert runtime.snapshot(static)["stage"]["stage"] is None


def test_the_stage_is_cleared_when_the_work_fails():
    static = runtime.static_snapshot("worker")
    with pytest.raises(RuntimeError):
        with runtime.stage("join_type"):
            raise RuntimeError("boom")
    assert runtime.snapshot(static)["stage"]["stage"] is None


def test_the_code_version_prefers_what_the_deploy_says(monkeypatch):
    monkeypatch.setenv("WARMR_CODE_VERSION", "abc1234")
    assert runtime.code_version() == "abc1234"
