"""Tests for connection reuse, conditional caching, and the re-check window."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from circle_leads.scraper import public_reader as pr
from circle_leads.scraper.http_client import shared_session


def test_shared_session_is_reused():
    """One pooled session is returned every time, so connections are reused."""
    assert shared_session() is shared_session()


def test_pooled_session_has_adapters():
    s = shared_session()
    assert "https://" in s.adapters and "http://" in s.adapters


class _Resp:
    def __init__(self, status, payload=None, headers=None):
        self.status_code = status
        self._payload = payload
        self.headers = headers or {}

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class _RecordingSession:
    """Fake session that records outgoing headers and returns scripted responses."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def get(self, url, headers=None, timeout=None):
        self.calls.append({"url": url, "headers": dict(headers or {})})
        return self.script.pop(0)


def test_conditional_request_sends_validators_and_reuses_body_on_304():
    pr._COND_CACHE.clear()
    # First response carries an ETag + body; second is a 304 (no body).
    sess = _RecordingSession([
        _Resp(200, {"records": [{"id": 1}]}, {"ETag": 'W/"abc"'}),
        _Resp(304, None, {}),
    ])
    reader = pr.PublicReader("example.circle.so", session=sess, requests_per_minute=100000)

    status1, body1 = reader._get("/internal_api/spaces")
    assert status1 == 200 and body1 == {"records": [{"id": 1}]}
    # No If-None-Match on the first call.
    assert "If-None-Match" not in sess.calls[0]["headers"]

    status2, body2 = reader._get("/internal_api/spaces")
    # Second call sent the validator, got 304, and reused the cached body.
    assert sess.calls[1]["headers"].get("If-None-Match") == 'W/"abc"'
    assert status2 == 200 and body2 == {"records": [{"id": 1}]}


def test_recheck_window_skips_recently_synced(monkeypatch):
    """A community synced within min_recheck_hours is skipped, not re-fetched."""
    import tempfile
    from circle_leads.storage.database import Database
    from circle_leads.storage.models import (
        Community, ActivityLog, AccessState, PermissionStatus,
    )
    from circle_leads.config.settings import Requirements
    from circle_leads import harvest as hv
    from sqlalchemy import select

    db = Database("sqlite:///" + tempfile.mktemp(suffix=".db"))
    with db.session() as s:
        c = Community(
            slug="x", name="X", url="https://x.circle.so",
            discovery_source="t", access_status=AccessState.NOT_VISITED.value,
            permission_status=PermissionStatus.CANDIDATE.value,
            relevance_score=50, relevant=True,
        )
        c.last_synced_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=1)
        s.add(c)

    # If the reader is constructed, the community was NOT skipped -> fail.
    def _boom(*a, **k):
        raise AssertionError("PublicReader should not be built for a skipped community")

    monkeypatch.setattr(hv, "PublicReader", _boom)

    res = hv.harvest(db, Requirements(), search=False, min_recheck_hours=6.0)
    assert res.communities_read == 0
    with db.session() as s:
        skipped = s.scalars(
            select(ActivityLog).where(ActivityLog.summary.like("%skipping%"))
        ).all()
    assert skipped, "expected a skip log entry"
