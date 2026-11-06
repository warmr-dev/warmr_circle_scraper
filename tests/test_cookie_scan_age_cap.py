"""A cookie scan stops reading at the post age cap.

2026-11-06: the private path read up to 100 posts per space plus their
comments on every pass and dropped the old ones only at triage; one pass over
the cookie communities took eight hours, asaporg alone almost four. No network
here: the reader's HTTP layer is replaced.
"""

from __future__ import annotations

import tempfile
from datetime import datetime, timedelta
from types import SimpleNamespace

from sqlalchemy import select

from circle_leads.storage.database import Database
from circle_leads.storage.models import CircleConnection
from tests.test_member_api_reader import _FakeHttp, _Resp

HOST = "x.circle.so"


def _post(pid, age):
    when = datetime.utcnow() - age
    return {"id": pid, "created_at": when.isoformat() + "Z", "name": f"Post {pid}",
            "body": f"Body of post {pid}, long enough to keep.", "url": f"/c/s/p{pid}"}


def _scan(monkeypatch, requirements, routes):
    monkeypatch.delenv("CIRCLE_CRED_KEY", raising=False)
    db = Database("sqlite:///" + tempfile.mktemp(suffix=".db"))
    from circle_leads.web.replay_store import connect_host
    connect_host(db, HOST, [{"name": "_circle_session", "value": "tok"}])

    from circle_leads.scraper import member_api_reader
    from circle_leads.triage import pipeline
    http = _FakeHttp(routes)
    monkeypatch.setattr(member_api_reader, "governed_session", lambda: http)
    monkeypatch.setattr(member_api_reader.time, "sleep", lambda _s: None)
    filed = []

    def triage(db_, records, requirements_, **kw):
        filed.extend(records)
        return SimpleNamespace(leads=[])
    monkeypatch.setattr(pipeline, "triage_records", triage)

    from circle_leads.scanning import scan_cookie_host
    out = scan_cookie_host(db, requirements, HOST)
    return out, http.requested, filed, db


ROUTES = {
    # Specific routes first: the fake matches by prefix.
    "/internal_api/spaces/1/posts?page=1&": _Resp(200, {
        "records": [_post(101, timedelta(hours=3)), _post(102, timedelta(days=10))],
        "has_next_page": True}),
    "/internal_api/spaces/1/posts?page=2&": _Resp(200, {
        "records": [_post(103, timedelta(days=20))], "has_next_page": True}),
    "/internal_api/spaces/1/posts?page=3&": _Resp(200, {
        "records": [_post(104, timedelta(days=30))], "has_next_page": False}),
    "/internal_api/posts/": _Resp(200, {"records": [], "has_next_page": False}),
    "/internal_api/spaces": _Resp(200, {"records": [{"id": 1, "slug": "s", "name": "S"}]}),
}


def test_a_first_read_stops_at_the_age_cap(monkeypatch, dev_requirements):
    dev_requirements.max_post_age_days = 2

    out, requested, filed, _db = _scan(monkeypatch, dev_requirements, ROUTES)

    assert out["state"] == "connected"
    assert any("/posts?page=2&" in p for p in requested)
    assert not any("/posts?page=3&" in p for p in requested)   # stopped at the first stale page
    comments = [p for p in requested if p.startswith("/internal_api/posts/")]
    assert comments and all("/posts/101/" in p for p in comments)   # only the fresh post
    assert [r for r in filed if "102" in str(r.get("url") or r.get("post_url") or "")] == []


def test_without_a_cap_a_first_read_goes_deep_as_before(monkeypatch, dev_requirements):
    dev_requirements.max_post_age_days = 0

    _out, requested, _filed, _db = _scan(monkeypatch, dev_requirements, ROUTES)

    assert any("/posts?page=3&" in p for p in requested)
    assert any("/posts/102/" in p for p in requested)


def test_an_empty_capped_read_says_so(monkeypatch, dev_requirements):
    dev_requirements.max_post_age_days = 2
    routes = dict(ROUTES)
    routes["/internal_api/spaces/1/posts?page=1&"] = _Resp(200, {
        "records": [_post(102, timedelta(days=10))], "has_next_page": True})

    out, requested, filed, db = _scan(monkeypatch, dev_requirements, routes)

    assert out["state"] == "connected" and filed == []
    assert not any("/posts?page=2&" in p for p in requested)
    with db.session() as s:
        conn = s.scalar(select(CircleConnection).where(CircleConnection.host == HOST))
        assert conn.state_detail == "No posts newer than 2 day(s) (1 space(s) visible)."


def test_age_since_is_off_at_zero(dev_requirements):
    from circle_leads.scanning import _age_since
    dev_requirements.max_post_age_days = 0
    assert _age_since(dev_requirements) is None
    dev_requirements.max_post_age_days = 2
    since = _age_since(dev_requirements)
    assert timedelta(hours=47) < datetime.utcnow() - since < timedelta(hours=49)
