"""Local Circle Connector: pairing/token auth, connection state, ingest.

No Circle credentials, cookies, or browser sessions touch any of this — the
connector uploads only normalized records. These tests use no network and no
real browser.
"""
from __future__ import annotations

import tempfile

import pytest
from fastapi.testclient import TestClient

from circle_leads.storage.database import Database
from circle_leads.storage.models import Connector, CircleConnection, ConnectionState
from circle_leads.web.connector_auth import (
    create_pairing, claim_pairing, verify_connector_token,
)
from sqlalchemy import select


def _db():
    return Database("sqlite:///" + tempfile.mktemp(suffix=".db"))


# --- pairing / token layer -------------------------------------------------

def test_pairing_roundtrip_issues_a_verifiable_token():
    db = _db()
    p = create_pairing(db, name="laptop")
    claimed = claim_pairing(db, p["pairing_code"])
    assert claimed and claimed["token"]
    c = verify_connector_token(db, claimed["token"])
    assert c is not None and c.paired is True


def test_pairing_code_is_one_time():
    db = _db()
    p = create_pairing(db)
    assert claim_pairing(db, p["pairing_code"]) is not None
    assert claim_pairing(db, p["pairing_code"]) is None  # burned


def test_bad_token_does_not_verify():
    db = _db()
    assert verify_connector_token(db, "nope") is None
    assert verify_connector_token(db, None) is None


def test_token_is_stored_only_as_hash():
    db = _db()
    p = create_pairing(db)
    claimed = claim_pairing(db, p["pairing_code"])
    with db.session() as s:
        row = s.scalar(select(Connector).where(Connector.id == p["connector_id"]))
        # The plaintext token is never stored; only its hash.
        assert row.token_hash and row.token_hash != claimed["token"]
        assert len(row.token_hash) == 64  # sha256 hex


# --- endpoints -------------------------------------------------------------

@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("DASHBOARD_PASSWORD", "testpassword")
    monkeypatch.setenv("DASHBOARD_SECRET_KEY", "k")
    from circle_leads.web.app import create_app
    db_url = "sqlite:///" + tempfile.mktemp(suffix=".db")
    c = TestClient(create_app(db_url=db_url))
    c.post("/login", data={"password": "testpassword"})
    return c


def test_connector_endpoints_require_token(client):
    # No bearer -> 401 on connector-authed routes.
    assert client.post("/api/connector/heartbeat").status_code == 401
    assert client.post("/api/connector/ingest", json={"host": "x.circle.so"}).status_code == 401


def test_pair_claim_heartbeat_flow(client):
    code = client.post("/api/connector/pair", json={"name": "laptop"}).json()["pairing_code"]
    token = client.post("/api/connector/claim", json={"code": code}).json()["token"]
    hdr = {"Authorization": f"Bearer {token}"}
    assert client.post("/api/connector/heartbeat", headers=hdr).json()["ok"] is True
    # dashboard sees it as online
    conns = client.get("/api/connectors").json()["connectors"]
    assert len(conns) == 1 and conns[0]["online"] is True


def test_connection_state_reported_and_listed(client):
    code = client.post("/api/connector/pair", json={}).json()["pairing_code"]
    token = client.post("/api/connector/claim", json={"code": code}).json()["token"]
    hdr = {"Authorization": f"Bearer {token}"}
    client.post("/api/connector/connections", headers=hdr, json={
        "host": "altea.circle.so", "state": ConnectionState.CONNECTED.value,
        "name": "Altea", "spaces_total": 12, "spaces_readable": 10,
    })
    rows = client.get("/api/connections").json()["connections"]
    assert rows and rows[0]["host"] == "altea.circle.so"
    assert rows[0]["state"] == "connected" and rows[0]["spaces_readable"] == 10


def test_ingest_classifies_and_counts(client):
    code = client.post("/api/connector/pair", json={}).json()["pairing_code"]
    token = client.post("/api/connector/claim", json={"code": code}).json()["token"]
    hdr = {"Authorization": f"Bearer {token}"}
    r = client.post("/api/connector/ingest", headers=hdr, json={
        "host": "altea.circle.so",
        "records": [{
            "source_content_id": "1", "content_type": "post", "thread_id": "1",
            "title": "Need a Flutter dev", "content": "We are hiring a Flutter developer, budget $10k.",
            "url": "https://altea.circle.so/c/x/1", "permission_reference": "browser_session",
        }],
    })
    assert r.status_code == 200 and r.json()["posts"] == 1


def test_add_and_remove_connection_via_dashboard(client):
    assert client.post("/api/connections/add", json={"host": "example.circle.so"}).json()["ok"]
    assert any(c["host"] == "example.circle.so"
               for c in client.get("/api/connections").json()["connections"])
    client.post("/api/connections/example.circle.so/remove")
    assert not any(c["host"] == "example.circle.so"
                   for c in client.get("/api/connections").json()["connections"])
