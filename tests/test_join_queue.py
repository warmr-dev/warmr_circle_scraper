"""Dashboard join queue: communities to join + connections needing a fresh
cookie. No network, no real Circle session -- an in-memory SQLite DB and the
FastAPI TestClient, same pattern as test_connector.py.
"""
from __future__ import annotations

import tempfile
from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from circle_leads.storage.database import Database, get_or_create_community
from circle_leads.storage.models import CircleConnection, ConnectionState


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("DASHBOARD_PASSWORD", "testpassword")
    monkeypatch.setenv("DASHBOARD_SECRET_KEY", "k")
    from circle_leads.web.app import create_app
    db_url = "sqlite:///" + tempfile.mktemp(suffix=".db")
    c = TestClient(create_app(db_url=db_url))
    c.post("/login", data={"password": "testpassword"})
    c._db_url = db_url
    return c


def _seed_community(client, slug, *, join_type, score=50, url=None):
    """Insert a Community row directly -- join_type is set by the harvest,
    not through any dashboard endpoint, so tests seed it the same way."""
    db = Database(client._db_url)
    with db.session() as s:
        c = get_or_create_community(s, slug=slug, url=url or f"https://{slug}.circle.so")
        c.join_type = join_type
        c.relevance_score = score
        c.join_type_checked_at = datetime.utcnow()


def test_join_queue_lists_free_and_paid_ranked_by_score(client):
    _seed_community(client, "low-free", join_type="free_join", score=20)
    _seed_community(client, "high-paid", join_type="paid", score=80)
    _seed_community(client, "closed", join_type="invite_only", score=90)
    _seed_community(client, "unclassified", join_type=None, score=95)

    rows = client.get("/api/join-queue").json()["to_join"]
    slugs = [r["slug"] for r in rows]
    assert slugs == ["high-paid", "low-free"]  # ranked, invite_only/None excluded
    assert {r["join_type"] for r in rows} == {"free_join", "paid"}


def test_join_queue_excludes_a_community_that_already_has_a_session(client):
    _seed_community(client, "founderscupid", join_type="free_join",
                     url="https://founderscupid.circle.so")
    client.post("/api/connections/founderscupid.circle.so/session",
                json={"session_cookie": "abc", "user_session_identifier": "def"})

    rows = client.get("/api/join-queue").json()["to_join"]
    assert not any(r["slug"] == "founderscupid" for r in rows)


def test_pasting_a_cookie_for_a_brand_new_host_auto_creates_the_connection(client):
    """No /api/connections/add call first -- pasting the cookie is enough for
    the worker's cookie_hosts_vip_first() to pick the host up next run."""
    r = client.post("/api/connections/new-host.circle.so/session",
                    json={"session_cookie": "abc", "user_session_identifier": "def"})
    assert r.status_code == 200

    rows = client.get("/api/connections").json()["connections"]
    row = next(c for c in rows if c["host"] == "new-host.circle.so")
    assert row["has_session"] is True
    assert row["state"] == ConnectionState.NOT_CONNECTED.value


def test_join_queue_lists_only_broken_connections_for_refresh(client):
    client.post("/api/connections/add", json={"host": "ok.circle.so"})
    client.post("/api/connections/add", json={"host": "expired.circle.so"})
    client.post("/api/connections/add", json={"host": "denied.circle.so"})

    db = Database(client._db_url)
    with db.session() as s:
        s.scalar(select(CircleConnection).where(CircleConnection.host == "ok.circle.so")
                 ).state = ConnectionState.CONNECTED.value
        s.scalar(select(CircleConnection).where(CircleConnection.host == "expired.circle.so")
                 ).state = ConnectionState.SESSION_EXPIRED.value
        s.scalar(select(CircleConnection).where(CircleConnection.host == "denied.circle.so")
                 ).state = ConnectionState.ACCESS_DENIED.value

    hosts = {r["host"] for r in client.get("/api/join-queue").json()["to_refresh"]}
    assert hosts == {"expired.circle.so", "denied.circle.so"}


# --- EXTENSION_API_TOKEN: the cookie-grabber extension's auth --------------

def _anon_client(monkeypatch, *, extension_token=None):
    """A TestClient that never logs in -- for testing the token-only path."""
    monkeypatch.setenv("DASHBOARD_PASSWORD", "testpassword")
    monkeypatch.setenv("DASHBOARD_SECRET_KEY", "k")
    if extension_token:
        monkeypatch.setenv("EXTENSION_API_TOKEN", extension_token)
    from circle_leads.web.app import create_app
    db_url = "sqlite:///" + tempfile.mktemp(suffix=".db")
    return TestClient(create_app(db_url=db_url))


def test_session_endpoint_rejects_no_auth_at_all(monkeypatch):
    anon = _anon_client(monkeypatch, extension_token="shh-secret")
    r = anon.post("/api/connections/ext-test.circle.so/session",
                  json={"session_cookie": "a", "user_session_identifier": "b"})
    assert r.status_code == 401


def test_session_endpoint_rejects_the_wrong_token(monkeypatch):
    anon = _anon_client(monkeypatch, extension_token="shh-secret")
    r = anon.post("/api/connections/ext-test.circle.so/session",
                  json={"session_cookie": "a", "user_session_identifier": "b"},
                  headers={"X-Extension-Token": "wrong"})
    assert r.status_code == 401


def test_session_endpoint_accepts_the_extension_token_without_login(monkeypatch):
    anon = _anon_client(monkeypatch, extension_token="shh-secret")
    r = anon.post("/api/connections/ext-test.circle.so/session",
                  json={"session_cookie": "a", "user_session_identifier": "b"},
                  headers={"X-Extension-Token": "shh-secret"})
    assert r.status_code == 200


def test_unset_extension_token_never_authenticates(monkeypatch):
    """No EXTENSION_API_TOKEN configured -> the header can't do anything,
    even if a caller happens to send a matching-looking value."""
    anon = _anon_client(monkeypatch, extension_token=None)
    r = anon.post("/api/connections/ext-test.circle.so/session",
                  json={"session_cookie": "a", "user_session_identifier": "b"},
                  headers={"X-Extension-Token": ""})
    assert r.status_code == 401


def test_extension_token_is_scoped_to_the_session_route_only(monkeypatch):
    """The token must not open up the rest of the dashboard -- it can store a
    cookie for a host of the caller's choosing, nothing else."""
    anon = _anon_client(monkeypatch, extension_token="shh-secret")
    r = anon.get("/api/join-queue", headers={"X-Extension-Token": "shh-secret"})
    assert r.status_code == 401
