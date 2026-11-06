"""A saved session is checked with Circle at once, and a session the server
cannot decrypt is reported as such -- not as an expired cookie.

2026-11-06: a pasted session left the community on "session expired" until the
worker's next read, so a good paste looked exactly like a bad one. No network
here: the reader's HTTP layer is replaced.
"""

from __future__ import annotations

import tempfile

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from circle_leads.scanning import verify_session as real_verify_session
from circle_leads.storage.database import Database
from circle_leads.storage.models import CircleConnection, ConnectionState, ReplaySession, ScanJob
from tests.test_member_api_reader import _FakeHttp, _Resp

HOST = "x.circle.so"
PASTE = {"session_cookie": "S", "user_session_identifier": "U"}


def _verdict(verdict, spaces=0, detail="d"):
    return lambda host, cookies, **kw: {"verdict": verdict, "spaces": spaces, "detail": detail}


@pytest.fixture
def dash(monkeypatch):
    monkeypatch.setenv("DASHBOARD_PASSWORD", "testpassword")
    monkeypatch.setenv("DASHBOARD_SECRET_KEY", "k")
    monkeypatch.setenv("CIRCLE_CRED_KEY", "test-key")
    from circle_leads.web.app import create_app
    db_url = "sqlite:///" + tempfile.mktemp(suffix=".db")
    c = TestClient(create_app(db_url=db_url))
    c.post("/login", data={"password": "testpassword"})
    return c, Database(db_url)


def _expire(db):
    with db.session() as s:
        conn = s.scalar(select(CircleConnection).where(CircleConnection.host == HOST))
        conn.state = ConnectionState.SESSION_EXPIRED.value
        conn.state_detail = "Session cookie invalid or expired -- refresh it."


def _conn(db):
    with db.session() as s:
        c = s.scalar(select(CircleConnection).where(CircleConnection.host == HOST))
        return c.state, c.state_detail


def _jobs(db):
    with db.session() as s:
        return [(j.kind, j.host, j.priority) for j in s.scalars(select(ScanJob)).all()]


# --- the save endpoint -----------------------------------------------------------

def test_a_working_paste_clears_session_expired_and_queues_a_read(dash, monkeypatch):
    c, db = dash
    c.post(f"/api/connections/{HOST}/session", json=PASTE)
    _expire(db)
    from circle_leads import scanning
    monkeypatch.setattr(scanning, "verify_session", _verdict("valid", 4, "Circle lists 4 space(s)."))

    r = c.post(f"/api/connections/{HOST}/session", json=PASTE)

    assert r.status_code == 200
    assert r.json()["check"]["verdict"] == "valid"
    state, detail = _conn(db)
    assert state == ConnectionState.CONNECTED.value
    assert "New session saved" in detail and "4 space(s)" in detail
    assert (("scan", HOST, 0)) in _jobs(db)


def test_a_rejected_paste_is_refused_and_the_stored_session_kept(dash, monkeypatch):
    c, db = dash
    c.post(f"/api/connections/{HOST}/session", json=PASTE)
    with db.session() as s:
        before = s.scalar(select(ReplaySession.encrypted_cookies).where(ReplaySession.host == HOST))
    from circle_leads import scanning
    monkeypatch.setattr(scanning, "verify_session", _verdict("rejected"))

    r = c.post(f"/api/connections/{HOST}/session",
               json={"session_cookie": "DEAD", "user_session_identifier": "DEAD"})

    assert r.status_code == 422
    assert "not saved" in r.json()["detail"] and "DEAD" not in r.json()["detail"]
    with db.session() as s:
        after = s.scalar(select(ReplaySession.encrypted_cookies).where(ReplaySession.host == HOST))
    assert after == before


def test_an_unchecked_paste_replaces_the_old_verdict_and_queues_a_read(dash):
    c, db = dash
    c.post(f"/api/connections/{HOST}/session", json=PASTE)
    _expire(db)

    r = c.post(f"/api/connections/{HOST}/session", json=PASTE)   # conftest: unverified

    assert r.status_code == 200
    state, detail = _conn(db)
    assert state == ConnectionState.NOT_CONNECTED.value
    assert "not checked in tests" in detail
    assert ("scan", HOST, 0) in _jobs(db)


def test_a_session_that_sees_no_spaces_is_not_called_working(dash, monkeypatch):
    c, db = dash
    from circle_leads import scanning
    monkeypatch.setattr(scanning, "verify_session", _verdict("no_spaces"))
    assert c.post(f"/api/connections/{HOST}/session", json=PASTE).status_code == 200
    assert _conn(db)[0] == ConnectionState.ACCESS_DENIED.value
    assert _jobs(db) == []


def test_a_partial_read_stays_marked_after_a_new_session(dash, monkeypatch):
    c, db = dash
    c.post(f"/api/connections/{HOST}/session", json=PASTE)
    with db.session() as s:
        conn = s.scalar(select(CircleConnection).where(CircleConnection.host == HOST))
        conn.state = ConnectionState.CONNECTED.value
        conn.state_detail = "40 post(s) from 2/9 space(s) (partial — scan again to continue)"
    from circle_leads import scanning
    monkeypatch.setattr(scanning, "verify_session", _verdict("valid", 9))
    c.post(f"/api/connections/{HOST}/session", json=PASTE)
    assert "partial" in _conn(db)[1]


# --- verify_session against Circle's answers ---------------------------------------

def _check(monkeypatch, routes):
    from circle_leads.scraper import member_api_reader
    monkeypatch.setattr(member_api_reader, "governed_session", lambda: _FakeHttp(routes))
    return real_verify_session(HOST, {"_circle_session": "COOKIE-AAA",
                                      "user_session_identifier": "COOKIE-BBB"})


def test_verify_reads_spaces_as_valid(monkeypatch):
    out = _check(monkeypatch, {"/internal_api/spaces": _Resp(200, {"records": [{"id": 1}, {"id": 2}]})})
    assert out["verdict"] == "valid" and out["spaces"] == 2


def test_verify_reads_401_as_rejected(monkeypatch):
    out = _check(monkeypatch, {"/internal_api/spaces": _Resp(401, {"error": "unauthorized"})})
    assert out["verdict"] == "rejected"


def test_verify_reads_no_json_as_unverified(monkeypatch):
    out = _check(monkeypatch, {})    # the SPA shell
    assert out["verdict"] == "unverified"


def test_verify_reads_an_empty_list_as_no_spaces(monkeypatch):
    out = _check(monkeypatch, {"/internal_api/spaces": _Resp(200, {"records": []})})
    assert out["verdict"] == "no_spaces"


@pytest.mark.parametrize("status", [200, 401])
def test_verify_never_echoes_cookie_values(monkeypatch, status):
    out = _check(monkeypatch, {"/internal_api/spaces": _Resp(status, {"records": [{"id": 1}]})})
    assert "COOKIE-AAA" not in str(out) and "COOKIE-BBB" not in str(out)


# --- a stored session this server cannot open --------------------------------------

def _scan_with_key(monkeypatch, store_key, scan_key, dev_requirements):
    db = Database("sqlite:///" + tempfile.mktemp(suffix=".db"))
    from circle_leads.web.replay_store import connect_host
    monkeypatch.setenv("CIRCLE_CRED_KEY", store_key)
    connect_host(db, HOST, [{"name": "_circle_session", "value": "S"}])
    if scan_key is None:
        monkeypatch.delenv("CIRCLE_CRED_KEY")
    else:
        monkeypatch.setenv("CIRCLE_CRED_KEY", scan_key)
    from circle_leads.scraper import member_api_reader
    sent = []

    def _no_http():
        http = _FakeHttp({"/internal_api/spaces": _Resp(401, {"error": "x"})})
        sent.append(http)
        return http
    monkeypatch.setattr(member_api_reader, "governed_session", _no_http)
    from circle_leads.scanning import scan_cookie_host
    return scan_cookie_host(db, dev_requirements, HOST), sent


@pytest.mark.skipif(
    __import__("importlib").util.find_spec("cryptography") is None,
    reason="cryptography not installed",
)
@pytest.mark.parametrize("scan_key", [None, "another-key"])
def test_an_undecryptable_session_is_an_error_not_an_expired_cookie(
        monkeypatch, dev_requirements, scan_key):
    out, sent = _scan_with_key(monkeypatch, "dashboard-key", scan_key, dev_requirements)
    assert out["state"] == ConnectionState.ERROR.value
    assert "CIRCLE_CRED_KEY" in out["detail"]
    assert not any(h.requested for h in sent)    # no cookieless request to Circle


# --- 403 is not proof of a dead cookie ----------------------------------------------

def test_verify_reads_403_as_forbidden_not_rejected(monkeypatch):
    out = _check(monkeypatch, {"/internal_api/spaces": _Resp(403, {"error": "forbidden"})})
    assert out["verdict"] == "forbidden"


def test_a_403_paste_is_stored_and_left_to_the_worker(dash, monkeypatch):
    c, db = dash
    from circle_leads import scanning
    monkeypatch.setattr(scanning, "verify_session", _verdict("forbidden", detail="403 from here"))

    r = c.post(f"/api/connections/{HOST}/session", json=PASTE)

    assert r.status_code == 200
    with db.session() as s:
        assert s.scalar(select(ReplaySession.id).where(ReplaySession.host == HOST)) is not None
    assert _conn(db)[0] == ConnectionState.ACCESS_DENIED.value
    assert ("scan", HOST, 0) in _jobs(db)


# --- the watcher's 401 belonged to the old cookie -----------------------------------

def _watch_refused(db):
    from circle_leads.storage.models import WatchState
    with db.session() as s:
        row = s.scalar(select(WatchState).where(WatchState.host == HOST))
        row.mode = "cookie"
        row.last_status = "unauthorized"
        row.last_detail = "spaces list: HTTP 401"


def _watch_status(db):
    from circle_leads.storage.models import WatchState
    with db.session() as s:
        return s.scalar(select(WatchState.last_status).where(WatchState.host == HOST))


def _needs_attention(c) -> set[str]:
    rules = c.get("/api/dash/attention").json()["rules"]
    rule = next(r for r in rules if r["key"] == "session_dead")
    return {item["key"].split(":", 1)[1] for item in rule["items"]}


def test_a_working_paste_clears_needs_attention_at_once(dash, monkeypatch):
    c, db = dash
    c.post(f"/api/connections/{HOST}/session", json=PASTE)
    _expire(db)
    _watch_refused(db)
    assert HOST in _needs_attention(c)
    from circle_leads import scanning
    monkeypatch.setattr(scanning, "verify_session", _verdict("valid", 3))

    c.post(f"/api/connections/{HOST}/session", json=PASTE)

    assert _watch_status(db) is None          # "not checked yet" until the watcher's next pass
    assert HOST not in _needs_attention(c)


def test_an_unchecked_paste_still_needs_verification(dash):
    c, db = dash
    c.post(f"/api/connections/{HOST}/session", json=PASTE)
    _expire(db)
    _watch_refused(db)

    c.post(f"/api/connections/{HOST}/session", json=PASTE)   # conftest: unverified

    assert _watch_status(db) == "unauthorized"
    assert HOST in _needs_attention(c)
