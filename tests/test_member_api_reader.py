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
    db_url = "sqlite:///" + _tempfile.mktemp(suffix=".db")
    c = _TC(create_app(db_url=db_url))
    c.post("/login", data={"password": "testpassword"})
    c._db_url = db_url  # no HTTP route exposes stored sessions; tests read the store directly
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
               json={"session_cookie": "_circle_session=SECRETVALUE",
                     "user_session_identifier": "USI_VALUE"})
    assert r.status_code == 200 and r.json()["cookies"] == 2
    # It's listed as a stored session (metadata only; the blob is encrypted).
    # No HTTP route exposes this (the Version B replay experiment that once did
    # was removed); read the store directly, same as replay_store's own tests.
    from circle_leads.storage.database import Database
    from circle_leads.web.replay_store import list_sessions
    sessions = list_sessions(Database(c._db_url))
    assert any(s["host"] == "x.circle.so" for s in sessions)


def test_store_session_works_without_encryption_key(monkeypatch):
    """No CIRCLE_CRED_KEY -> stored as plaintext (the user's choice), not refused."""
    monkeypatch.setenv("DASHBOARD_PASSWORD", "testpassword")
    monkeypatch.setenv("DASHBOARD_SECRET_KEY", "k")
    monkeypatch.delenv("CIRCLE_CRED_KEY", raising=False)
    from circle_leads.web.app import create_app
    c = _TC(create_app(db_url="sqlite:///" + _tempfile.mktemp(suffix=".db")))
    c.post("/login", data={"password": "testpassword"})
    c.post("/api/connections/add", json={"host": "x.circle.so"})
    r = c.post("/api/connections/x.circle.so/session",
               json={"session_cookie": "v", "user_session_identifier": "u"})
    assert r.status_code == 200 and r.json()["cookies"] == 2


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


def test_store_session_one_cookie_saves_but_warns(monkeypatch):
    """One cookie is stored but the response lists what's still missing."""
    c = _dash(monkeypatch)
    c.post("/api/connections/add", json={"host": "x.circle.so"})
    r = c.post("/api/connections/x.circle.so/session",
               json={"session_cookie": "s"})
    assert r.status_code == 200
    assert "user_session_identifier" in r.json()["missing"]


def test_store_session_option_b_two_values(monkeypatch):
    """Option B: the two values pasted individually (OR the JSON export)."""
    c = _dash(monkeypatch)
    c.post("/api/connections/add", json={"host": "x.circle.so"})
    r = c.post("/api/connections/x.circle.so/session",
               json={"session_cookie": "S", "user_session_identifier": "U"})
    assert r.status_code == 200 and r.json()["cookies"] == 2 and r.json()["missing"] == []


def test_store_session_accepts_full_cookie_json_export(monkeypatch):
    """The easy path: paste the whole cookie export; both are extracted."""
    c = _dash(monkeypatch)
    c.post("/api/connections/add", json={"host": "x.circle.so"})
    export = [
        {"domain": "x.circle.so", "name": "_circle_session", "value": "S"},
        {"domain": "x.circle.so", "name": "user_session_identifier", "value": "U"},
        {"domain": "x.circle.so", "name": "cf_clearance", "value": "CF"},  # ignored
        {"domain": ".x.circle.so", "name": "_ga", "value": "A"},           # ignored
    ]
    import json
    r = c.post("/api/connections/x.circle.so/session", json={"cookies": json.dumps(export)})
    assert r.status_code == 200
    assert r.json()["cookies"] == 2   # only the two session cookies stored


def test_connection_reports_has_session_flag(monkeypatch):
    c = _dash(monkeypatch)
    c.post("/api/connections/add", json={"host": "a.circle.so"})
    assert c.get("/api/connections").json()["connections"][0]["has_session"] is False
    import json
    export = [{"domain": "a.circle.so", "name": "_circle_session", "value": "S"},
              {"domain": "a.circle.so", "name": "user_session_identifier", "value": "U"}]
    c.post("/api/connections/a.circle.so/session", json={"cookies": json.dumps(export)})
    assert c.get("/api/connections").json()["connections"][0]["has_session"] is True


def test_clear_session_keeps_the_community(monkeypatch):
    c = _dash(monkeypatch)
    c.post("/api/connections/add", json={"host": "a.circle.so"})
    import json
    export = [{"domain": "a.circle.so", "name": "_circle_session", "value": "S"},
              {"domain": "a.circle.so", "name": "user_session_identifier", "value": "U"}]
    c.post("/api/connections/a.circle.so/session", json={"cookies": json.dumps(export)})
    c.post("/api/connections/a.circle.so/session/clear")
    conns = c.get("/api/connections").json()["connections"]
    assert len(conns) == 1 and conns[0]["has_session"] is False   # community stays


def test_removing_a_community_drops_its_stored_session(monkeypatch):
    c = _dash(monkeypatch)
    c.post("/api/connections/add", json={"host": "a.circle.so"})
    import json
    export = [{"domain": "a.circle.so", "name": "_circle_session", "value": "S"},
              {"domain": "a.circle.so", "name": "user_session_identifier", "value": "U"}]
    c.post("/api/connections/a.circle.so/session", json={"cookies": json.dumps(export)})
    c.post("/api/connections/a.circle.so/remove")
    from circle_leads.storage.database import Database
    from circle_leads.web.replay_store import list_sessions
    assert list_sessions(Database(c._db_url)) == []   # no orphan credential


def test_clear_session_requires_auth(monkeypatch):
    monkeypatch.setenv("DASHBOARD_PASSWORD", "testpassword")
    monkeypatch.setenv("DASHBOARD_SECRET_KEY", "k")
    from circle_leads.web.app import create_app
    anon = _TC(create_app(db_url="sqlite:///" + _tempfile.mktemp(suffix=".db")))
    assert anon.post("/api/connections/a.circle.so/session/clear").status_code == 401


# --- comments + replies + tiptap extraction --------------------------------

def test_tiptap_body_flattens_to_full_text():
    from circle_leads.scraper.member_api_reader import _tiptap_text
    body = {"type": "doc", "content": [
        {"type": "paragraph", "content": [{"type": "text", "text": "We are hiring a "},
                                           {"type": "text", "text": "backend engineer."}]},
        {"type": "paragraph", "content": [{"type": "text", "text": "DM me."}]},
    ]}
    text = _tiptap_text(body)
    assert "We are hiring a backend engineer." in text
    assert "DM me." in text


def test_extract_text_prefers_full_tiptap_over_truncated():
    from circle_leads.scraper.member_api_reader import _extract_text
    rec = {
        "name": "Job",
        "truncated_content": "We are hiring a…",     # preview
        "tiptap_body": {"type": "doc", "content": [
            {"type": "paragraph", "content": [
                {"type": "text", "text": "We are hiring a senior C++ engineer, remote, contract."}]}]},
    }
    title, body = _extract_text(rec)
    assert title == "Job"
    assert "senior C++ engineer, remote, contract" in body   # full, not truncated


def test_fetch_space_posts_includes_comments_and_replies():
    # A post with a comment that has a reply.
    routes = {
        "/internal_api/spaces/9/posts": _Resp(200, {"records": [
            {"id": 100, "name": "Post", "truncated_content": "Body.", "comments_count": 1}
        ], "has_next_page": False}),
        "/internal_api/posts/100/comments": _Resp(200, {"records": [
            {"id": 200, "truncated_content": "We're looking for a dev, DM me.",
             "replies_count": 1, "community_member": {"id": 5, "name": "Ann"}}
        ], "has_next_page": False}),
        "/internal_api/comments/200/comments": _Resp(200, {"records": [
            {"id": 300, "truncated_content": "Sent you a message.",
             "community_member": {"id": 6, "name": "Bob"}}
        ]}),
    }
    r = _reader(routes)
    recs = fetch_space_posts(r, 9, with_comments=True)
    kinds = [x["content_type"] for x in recs]
    assert kinds.count("post") == 1
    assert kinds.count("comment") == 2   # the comment + its reply
    comment = next(x for x in recs if x["content_type"] == "comment" and "looking for" in x["content"])
    assert comment["thread_id"] == "100"        # tied to its post
    assert comment["author"]["display_name"] == "Ann"


def test_comments_can_be_disabled():
    routes = {
        "/internal_api/spaces/9/posts": _Resp(200, {"records": [
            {"id": 100, "name": "P", "truncated_content": "B", "comments_count": 3}
        ], "has_next_page": False}),
    }
    r = _reader(routes)
    recs = fetch_space_posts(r, 9, with_comments=False)
    assert all(x["content_type"] == "post" for x in recs)


def test_extract_text_reads_the_wrapped_tiptap_body_circle_sends():
    # The list response wraps the document -- tiptap_body = {"body": doc,
    # "circle_ios_fallback_text": ..., ...} -- and the walk used to stop at the
    # wrapper, so every cookie-read post kept its preview (productizeyourself,
    # 2026-09-19: 252 of 1,039 chars).
    from circle_leads.scraper.member_api_reader import _extract_text

    full = "Intro about our startup. " * 20 + "We are hiring a senior backend developer."
    rec = {
        "name": "Update",
        "truncated_content": full[:255],
        "tiptap_body": {
            "body": {"type": "doc", "content": [
                {"type": "paragraph", "content": [{"type": "text", "text": full}]}]},
            "circle_ios_fallback_text": full,
            "attachments": [],
        },
    }
    _, body = _extract_text(rec)
    assert "hiring a senior backend developer" in body


def test_extract_text_keeps_the_preview_when_the_walk_finds_less():
    from circle_leads.scraper.member_api_reader import _extract_text

    rec = {"name": "t", "truncated_content": "We need a React developer for 3 months",
           "tiptap_body": {"body": {"type": "doc", "content": []}}}
    _, body = _extract_text(rec)
    assert body == "We need a React developer for 3 months"


def test_tiptap_mentions_and_post_links_keep_their_text():
    # Mentions and links to posts/events are leaf nodes without a text child;
    # their visible text is circle_ios_fallback_text.
    from circle_leads.scraper.member_api_reader import _tiptap_text

    doc = {"type": "doc", "content": [{"type": "paragraph", "content": [
        {"type": "text", "text": "Thanks "},
        {"type": "mention", "attrs": {"sgid": "x"}, "circle_ios_fallback_text": "@Jane Doe"},
        {"type": "text", "text": ", see "},
        {"type": "entity", "attrs": {"sgid": "y"}, "circle_ios_fallback_text": "#Hiring board"},
    ]}]}
    text = _tiptap_text(doc)
    assert "@Jane Doe" in text and "#Hiring board" in text


# --- a join whose new-member profile step is still open --------------------

def test_profile_step_still_open_is_reported_not_read_as_no_spaces():
    # Confirmed live 2026-09-18: until "Create a profile" is saved, Circle
    # answers every API call with this 400. It used to come back as None, so
    # the scan wrote "connected, 0 spaces" -- a silent failure.
    from circle_leads.scraper.member_api_reader import ProfileIncomplete

    routes = {"/internal_api/spaces": _Resp(
        400, {"success": False, "message": "Please confirm before proceeding", "error_details": []})}
    with pytest.raises(ProfileIncomplete, match="profile step"):
        _reader(routes).list_spaces()


def test_any_other_400_still_reads_as_nothing():
    routes = {"/internal_api/spaces": _Resp(400, {"success": False, "message": "Bad request"})}
    assert _reader(routes).list_spaces() == []


def test_scan_reports_an_unfinished_join_as_an_error_with_the_reason(monkeypatch):
    import tempfile

    from sqlalchemy import select

    from circle_leads.config.settings import load_requirements
    from circle_leads.scanning import scan_cookie_host
    from circle_leads.scraper import member_api_reader
    from circle_leads.storage.database import Database
    from circle_leads.storage.models import CircleConnection
    from circle_leads.web import replay_store

    db = Database("sqlite:///" + tempfile.mktemp(suffix=".db"))
    monkeypatch.setattr(replay_store, "load_cookies",
                        lambda db_, host: [{"name": "_circle_session", "value": "tok"}])

    def fake_list_spaces(self):
        raise member_api_reader.ProfileIncomplete("Circle answered 400: the join is not finished")

    monkeypatch.setattr(member_api_reader.MemberApiReader, "list_spaces", fake_list_spaces)

    out = scan_cookie_host(db, load_requirements(), "x.circle.so")

    assert out["state"] == "error"
    with db.session() as s:
        conn = s.scalar(select(CircleConnection).where(CircleConnection.host == "x.circle.so"))
        assert conn.state == "error"
        assert "join is not finished" in conn.state_detail


# --- a re-read takes only what is new ------------------------------------------
#
# One pass over the ~40 stored sessions took eleven hours on 2026-09-23/24: the
# posts list carries no comments_count, so every scan fetched the comments of
# the first 25 posts of every space again, and paged through old posts too.

def test_a_reread_stops_paging_and_skips_the_comments_of_old_posts():
    from datetime import datetime

    new = {"id": 1, "name": "New", "truncated_content": "fresh", "created_at": "2026-09-24T10:00:00Z"}
    old = {"id": 2, "name": "Old", "truncated_content": "stale", "created_at": "2026-08-01T10:00:00Z"}
    older = {"id": 3, "name": "Older", "truncated_content": "stale", "created_at": "2026-07-01T10:00:00Z"}
    routes = {
        "/internal_api/spaces/9/posts?page=1": _Resp(200, {"records": [new, old], "has_next_page": True}),
        "/internal_api/spaces/9/posts?page=2": _Resp(200, {"records": [older], "has_next_page": True}),
        "/internal_api/spaces/9/posts?page=3": _Resp(200, {"records": [older], "has_next_page": False}),
        "/internal_api/posts/": _Resp(200, {"records": [], "has_next_page": False}),
    }
    r = _reader(routes)
    r.request_pause = 0
    recs = fetch_space_posts(r, 9, since=datetime(2026, 9, 17))
    assert [x["title"] for x in recs] == ["New"]
    asked = r._http.requested
    # Page 2 held nothing new, so page 3 was never asked for.
    assert not any(p.startswith("/internal_api/spaces/9/posts?page=3") for p in asked)
    assert any(p.startswith("/internal_api/posts/1/comments") for p in asked)
    assert not any(p.startswith("/internal_api/posts/2/comments") for p in asked)


def test_a_first_read_still_takes_everything():
    old = {"id": 2, "name": "Old", "truncated_content": "stale", "created_at": "2026-08-01T10:00:00Z"}
    routes = {
        "/internal_api/spaces/9/posts?page=1": _Resp(200, {"records": [old], "has_next_page": False}),
        "/internal_api/posts/": _Resp(200, {"records": [], "has_next_page": False}),
    }
    r = _reader(routes)
    r.request_pause = 0
    assert [x["title"] for x in fetch_space_posts(r, 9)] == ["Old"]


def _conn_db():
    import tempfile

    from circle_leads.storage.database import Database
    return Database("sqlite:///" + tempfile.mktemp(suffix=".db"))


@pytest.mark.parametrize("state,detail,readable,expect_since", [
    ("connected", "40 post(s), 0 lead(s) from 3/3 space(s)", 3, True),
    ("connected", "12 post(s) ... (partial — scan again to continue)", 3, False),
    ("connected", "No readable posts (3 space(s) visible).", 0, False),
    ("error", "Session rejected", 3, False),
])
def test_a_reread_starts_from_the_last_complete_scan_only(state, detail, readable, expect_since):
    from datetime import datetime, timedelta

    from circle_leads.scanning import RESCAN_OVERLAP, _rescan_since
    from circle_leads.storage.models import CircleConnection

    db = _conn_db()
    at = datetime(2026, 9, 24, 6, 0)
    with db.session() as s:
        s.add(CircleConnection(host="x.circle.so", state=state, state_detail=detail,
                               spaces_readable=readable, last_sync_at=at))
    since = _rescan_since(db, "x.circle.so")
    assert since == (at - RESCAN_OVERLAP if expect_since else None)
    assert RESCAN_OVERLAP == timedelta(days=7)


def test_a_complete_member_scan_counts_as_a_read(monkeypatch):
    """community.freelancemvp.com was read every six hours with its session and
    still showed on the dashboard as never read."""
    from sqlalchemy import select

    from circle_leads.config.settings import load_requirements
    from circle_leads.scanning import scan_cookie_host
    from circle_leads.scraper import member_api_reader
    from circle_leads.storage.database import get_or_create_community
    from circle_leads.storage.models import Community
    from circle_leads.web import replay_store

    db = _conn_db()
    with db.session() as s:
        get_or_create_community(s, slug="x", url="https://x.circle.so")
    monkeypatch.setattr(replay_store, "load_cookies",
                        lambda db_, host: [{"name": "_circle_session", "value": "tok"}])
    monkeypatch.setattr(member_api_reader.MemberApiReader, "list_spaces",
                        lambda self: [{"id": 1, "slug": "general", "name": "General"}])
    monkeypatch.setattr(member_api_reader, "fetch_space_posts", lambda *a, **kw: [])

    out = scan_cookie_host(db, load_requirements(), "x.circle.so")

    assert out["state"] == "connected"
    with db.session() as s:
        assert s.scalar(select(Community.last_synced_at).where(Community.slug == "x")) is not None
