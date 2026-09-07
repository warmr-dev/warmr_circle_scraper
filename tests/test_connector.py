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


@pytest.fixture
def connector_token(client):
    """A paired connector's bearer token, for connector-authed routes."""
    code = client.post("/api/connector/pair", json={}).json()["pairing_code"]
    return client.post("/api/connector/claim", json={"code": code}).json()["token"]


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


# --- scan priority (VIP) ---------------------------------------------------

def test_priority_defaults_to_normal_and_round_trips(client):
    client.post("/api/connections/add", json={"host": "a.circle.so"})
    rows = client.get("/api/connections").json()["connections"]
    assert rows[0]["priority"] == "normal"

    r = client.post("/api/connections/a.circle.so/priority", json={"priority": "vip"})
    assert r.status_code == 200 and r.json()["priority"] == "vip"
    rows = client.get("/api/connections").json()["connections"]
    assert rows[0]["priority"] == "vip"


def test_add_accepts_priority_and_notes(client):
    client.post("/api/connections/add", json={
        "host": "b.circle.so", "priority": "vip", "notes": "biggest client"})
    row = client.get("/api/connections").json()["connections"][0]
    assert row["priority"] == "vip"
    assert row["notes"] == "biggest client"


def test_re_adding_a_host_updates_it_rather_than_duplicating(client):
    a = client.post("/api/connections/add", json={"host": "c.circle.so"})
    assert a.json()["created"] is True
    b = client.post("/api/connections/add",
                    json={"host": "c.circle.so", "priority": "vip"})
    assert b.json()["created"] is False
    rows = client.get("/api/connections").json()["connections"]
    assert len(rows) == 1 and rows[0]["priority"] == "vip"


def test_connections_are_listed_vip_first(client):
    for host, prio in [("low.circle.so", "low"), ("norm.circle.so", "normal"),
                       ("vip.circle.so", "vip"), ("paused.circle.so", "paused")]:
        client.post("/api/connections/add", json={"host": host, "priority": prio})
    order = [c["host"] for c in client.get("/api/connections").json()["connections"]]
    assert order == ["vip.circle.so", "norm.circle.so",
                     "low.circle.so", "paused.circle.so"]


def test_bad_priority_is_rejected(client):
    client.post("/api/connections/add", json={"host": "d.circle.so"})
    assert client.post("/api/connections/d.circle.so/priority",
                       json={"priority": "urgent"}).status_code == 400
    assert client.post("/api/connections/add",
                       json={"host": "e.circle.so", "priority": "nope"}).status_code == 400


def test_priority_on_an_unknown_host_is_404(client):
    assert client.post("/api/connections/ghost.circle.so/priority",
                       json={"priority": "vip"}).status_code == 404


def test_add_normalizes_a_pasted_url(client):
    client.post("/api/connections/add", json={"host": "https://Weird.circle.so/c/general/"})
    assert client.get("/api/connections").json()["connections"][0]["host"] == "weird.circle.so"


def test_notes_can_be_set_and_cleared(client):
    client.post("/api/connections/add", json={"host": "f.circle.so"})
    client.post("/api/connections/f.circle.so/notes", json={"notes": "warm intro via Sam"})
    assert client.get("/api/connections").json()["connections"][0]["notes"] == "warm intro via Sam"
    client.post("/api/connections/f.circle.so/notes", json={"notes": ""})
    assert client.get("/api/connections").json()["connections"][0]["notes"] is None


# --- the worklist the connector polls --------------------------------------

def test_worklist_is_priority_ordered_and_omits_paused(client, connector_token):
    for host, prio in [("z-low.circle.so", "low"), ("m-norm.circle.so", "normal"),
                       ("a-vip.circle.so", "vip"), ("p.circle.so", "paused")]:
        client.post("/api/connections/add", json={"host": host, "priority": prio})

    r = client.get("/api/connector/worklist",
                   headers={"Authorization": f"Bearer {connector_token}"})
    assert r.status_code == 200
    hosts = [c["host"] for c in r.json()["communities"]]
    assert hosts == ["a-vip.circle.so", "m-norm.circle.so", "z-low.circle.so"]
    assert "p.circle.so" not in hosts


def test_worklist_requires_a_connector_token(client):
    assert client.get("/api/connector/worklist").status_code == 401
    assert client.get("/api/connector/worklist",
                      headers={"Authorization": "Bearer wrong"}).status_code == 401
