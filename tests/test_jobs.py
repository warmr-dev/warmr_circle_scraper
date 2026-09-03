"""Tests for the background job registry."""

import time

from circle_leads.web.jobs import JobRegistry


def test_job_runs_and_completes():
    reg = JobRegistry()
    done = []
    job = reg.start("search", "test", lambda j: done.append(j.id))
    for _ in range(20):
        if reg.get(job.id).state != "running":
            break
        time.sleep(0.05)
    assert reg.get(job.id).state == "done"
    assert done == [job.id]


def test_job_error_is_captured():
    reg = JobRegistry()

    def boom(job):
        raise ValueError("kaboom")

    job = reg.start("read", "test", boom)
    for _ in range(20):
        if reg.get(job.id).state != "running":
            break
        time.sleep(0.05)
    j = reg.get(job.id)
    assert j.state == "error"
    assert "kaboom" in j.detail


def test_active_reflects_running_state():
    reg = JobRegistry()
    ev = []

    def slow(job):
        while not ev:
            time.sleep(0.01)

    job = reg.start("search", "slow", slow)
    assert reg.active("search") is True
    assert reg.active("read") is False
    ev.append(1)  # let it finish
    for _ in range(20):
        if not reg.active():
            break
        time.sleep(0.05)
    assert reg.active() is False


def test_registry_trims_old_jobs():
    reg = JobRegistry(max_jobs=3)
    for i in range(6):
        reg.start("search", f"j{i}", lambda j: None)
    time.sleep(0.2)
    assert len(reg.list(limit=100)) <= 3
