"""HTTP member-API reader: reads /internal_api with a session cookie, no browser.

The key property: Circle's /internal_api JSON endpoints are not behind the
Cloudflare challenge that gates the HTML pages, so a session cookie + a plain
HTTP client reads a private community. These tests mock the HTTP layer so they
run offline and deterministically.
"""

from __future__ import annotations

import json

import pytest

from circle_leads.scraper.member_api_reader import (
    ChallengeHit, MemberApiReader, SessionInvalid, fetch_space_posts,
)


class _Resp:
    def __init__(self, status, body, json_ct=True):
        self.status_code = status
        self._body = body
        self.headers = {"content-type": "application/json" if json_ct else "text/html"}

    @property
    def text(self):
        return self._body if isinstance(self._body, str) else json.dumps(self._body)

    def json(self):
        if isinstance(self._body, str):
            raise ValueError("not json")
        return self._body


class _FakeHttp:
    """Maps path -> _Resp. Records requests."""

    def __init__(self, routes):
        self.routes = routes
        self.headers = {}
        self.cookies = _CookieJar()
        self.requested = []

    def get(self, url, timeout=None, allow_redirects=None):
        # Strip scheme+host to get the path (+query).
        path = "/" + url.split("://", 1)[-1].split("/", 1)[1] if "://" in url else url
        self.requested.append(path)
        for prefix, resp in self.routes.items():
            if path.startswith(prefix):
                return resp
        return _Resp(404, "<!DOCTYPE html>", json_ct=False)


class _CookieJar:
    def __init__(self):
        self.jar = {}

    def set(self, name, value, domain=None):
        self.jar[name] = value


def _reader(routes, cookies=None):
    r = MemberApiReader("x.circle.so", cookies=cookies or {"_circle_session": "tok"})
    r._http = _FakeHttp(routes)
    return r


# --- session handling ------------------------------------------------------

def test_valid_session_lists_spaces():
    routes = {"/internal_api/spaces": _Resp(200, {"records": [
        {"id": 1, "slug": "general", "name": "General", "space_type": "post"},
        {"id": 2, "slug": "jobs", "name": "Jobs"},
    ]})}
    r = _reader(routes)
    assert r.check_session() is True
    spaces = r.list_spaces()
    assert [s["slug"] for s in spaces] == ["general", "jobs"]
    assert spaces[0]["id"] == "1"


def test_expired_session_raises_and_check_returns_false():
    routes = {"/internal_api/spaces": _Resp(401, {"message": "You cannot perform this action."})}
    r = _reader(routes)
    assert r.check_session() is False
    with pytest.raises(SessionInvalid):
        r.list_spaces()


def test_an_unexpected_challenge_is_reported_not_bypassed():
    routes = {"/internal_api/spaces": _Resp(
        200, "<html>__cf_chl challenge-platform</html>", json_ct=False)}
    r = _reader(routes)
    with pytest.raises(ChallengeHit):
        r.list_spaces()


# --- reads -----------------------------------------------------------------

def test_list_posts_paginates_until_no_next_page():
    routes = {
        "/internal_api/spaces/9/posts": _Resp(200, {
            "records": [{"id": 100, "name": "Hiring a backend dev",
                         "truncated_content": "We need someone in Python."}],
            "has_next_page": False,
        }),
    }
    r = _reader(routes)
    posts = r.list_posts(9, max_pages=5)
    assert len(posts) == 1 and posts[0]["id"] == 100


def test_fetch_space_posts_normalizes_to_pipeline_shape():
    routes = {
        "/internal_api/spaces/9/posts": _Resp(200, {
            "records": [{
                "id": 100, "name": "Looking for a C++ engineer",
                "truncated_content": "Contractor, remote.",
                "url": "/c/jobs/looking",
                "created_at": "2026-09-01T10:00:00Z",
                "user": {"id": 5, "name": "Dana"},
            }],
            "has_next_page": False,
        }),
    }
    r = _reader(routes)
    recs = fetch_space_posts(r, 9)
    assert len(recs) == 1
    rec = recs[0]
    assert rec["source_content_id"] == "100"
    assert rec["content_type"] == "post"
    assert "C++ engineer" in rec["title"]
    assert rec["url"] == "https://x.circle.so/c/jobs/looking"   # relative -> absolute
    assert rec["author"]["display_name"] == "Dana"
    assert rec["permission_reference"] == "member_session"      # traces the auth source


def test_cookie_values_are_never_in_repr():
    r = MemberApiReader("x.circle.so", cookies={"_circle_session": "SUPER_SECRET_VALUE"})
    assert "SUPER_SECRET_VALUE" not in repr(r)
    assert "redacted" in repr(r)


def test_only_session_cookies_are_sent_not_cf_or_analytics():
    r = MemberApiReader("x.circle.so", cookies={
        "_circle_session": "s", "cf_clearance": "CF", "_ga": "analytics",
    })
    jar = r._http.cookies
    # requests' real jar; assert cf_clearance / _ga were not added.
    names = {c.name for c in jar}
    assert "_circle_session" in names
    assert "cf_clearance" not in names   # API doesn't need it; don't ship it
    assert "_ga" not in names


# --- dashboard endpoints: store cookie (encrypted) + HTTP scan --------------

import tempfile as _tempfile
from fastapi.testclient import TestClient as _TC


def _dash(monkeypatch):
    monkeypatch.setenv("DASHBOARD_PASSWORD", "testpassword")
    monkeypatch.setenv("DASHBOARD_SECRET_KEY", "k")
    monkeypatch.setenv("CIRCLE_CRED_KEY", "test-key")
    from circle_leads.web.app import create_app
    c = _TC(create_app(db_url="sqlite:///" + _tempfile.mktemp(suffix=".db")))
    c.post("/login", data={"password": "testpassword"})
    return c


def test_store_session_cookie_requires_a_value(monkeypatch):
    c = _dash(monkeypatch)
    c.post("/api/connections/add", json={"host": "x.circle.so"})
    assert c.post("/api/connections/x.circle.so/session",
                  json={"session_cookie": ""}).status_code == 400


def test_store_session_cookie_strips_name_prefix_and_encrypts(monkeypatch):
    c = _dash(monkeypatch)
    c.post("/api/connections/add", json={"host": "x.circle.so"})
    r = c.post("/api/connections/x.circle.so/session",
               json={"session_cookie": "_circle_session=SECRETVALUE"})
    assert r.status_code == 200 and r.json()["cookies"] == 1
    # It's listed as a stored session (metadata only; the blob is encrypted).
    sessions = c.get("/api/replay/sessions").json()["sessions"]
    assert any(s["host"] == "x.circle.so" for s in sessions)


def test_store_session_refuses_without_encryption_key(monkeypatch):
    monkeypatch.setenv("DASHBOARD_PASSWORD", "testpassword")
    monkeypatch.setenv("DASHBOARD_SECRET_KEY", "k")
    monkeypatch.delenv("CIRCLE_CRED_KEY", raising=False)
    from circle_leads.web.app import create_app
    c = _TC(create_app(db_url="sqlite:///" + _tempfile.mktemp(suffix=".db")))
    c.post("/login", data={"password": "testpassword"})
    c.post("/api/connections/add", json={"host": "x.circle.so"})
    r = c.post("/api/connections/x.circle.so/session",
               json={"session_cookie": "v"})
    assert r.status_code == 400
    assert "CIRCLE_CRED_KEY" in r.json()["detail"]


def test_scan_without_a_stored_session_is_404(monkeypatch):
    c = _dash(monkeypatch)
    c.post("/api/connections/add", json={"host": "x.circle.so"})
    assert c.post("/api/connections/x.circle.so/scan").status_code == 404


def test_scan_endpoints_require_auth(monkeypatch):
    monkeypatch.setenv("DASHBOARD_PASSWORD", "testpassword")
    monkeypatch.setenv("DASHBOARD_SECRET_KEY", "k")
    from circle_leads.web.app import create_app
    anon = _TC(create_app(db_url="sqlite:///" + _tempfile.mktemp(suffix=".db")))
    assert anon.post("/api/connections/x.circle.so/session", json={}).status_code == 401
    assert anon.post("/api/connections/x.circle.so/scan").status_code == 401
