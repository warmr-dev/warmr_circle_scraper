"""Durable scan-job queue: enqueue/dedup, VIP-first atomic claim, complete."""
from __future__ import annotations

import tempfile
import pytest

from circle_leads.storage.database import Database
from circle_leads.storage.job_queue import enqueue, claim_next, complete, recent
from circle_leads.storage.models import JobState


def _db():
    return Database("sqlite:///" + tempfile.mktemp(suffix=".db"))


def test_enqueue_dedups_queued_jobs():
    db = _db()
    a = enqueue(db, "scan", host="x.circle.so")
    b = enqueue(db, "scan", host="x.circle.so")   # identical, still queued
    assert a == b


def test_claim_takes_vip_priority_first():
    db = _db()
    enqueue(db, "scan", host="normal.circle.so", priority=1)
    enqueue(db, "scan", host="vip.circle.so", priority=0)
    job = claim_next(db)
    assert job.host == "vip.circle.so"
    assert job.state == JobState.RUNNING.value


def test_claim_returns_none_when_empty():
    assert claim_next(_db()) is None


def test_full_lifecycle():
    db = _db()
    jid = enqueue(db, "scan_all")
    job = claim_next(db)
    assert job.id == jid
    complete(db, jid, result={"leads": 5, "detail": "ok"})
    rows = recent(db)
    assert rows[0]["state"] == JobState.DONE.value
    assert rows[0]["result"]["leads"] == 5
    # nothing left to claim
    assert claim_next(db) is None


def test_error_completion_records_message():
    db = _db()
    jid = enqueue(db, "scan", host="x.circle.so")
    claim_next(db)
    complete(db, jid, result={}, error="BoomError: nope")
    row = recent(db)[0]
    assert row["state"] == JobState.ERROR.value
    assert "BoomError" in row["detail"]
