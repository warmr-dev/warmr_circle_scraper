"""Tests for the dashboard: auth gate, API surface, and activity logging."""

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from circle_leads.config.settings import load_requirements  # noqa: E402
from circle_leads.storage.database import Database  # noqa: E402
from circle_leads.triage.pipeline import triage_text  # noqa: E402
from circle_leads.web.auth import AuthNotConfigured, verify_password  # noqa: E402

PASSWORD = "dashboard-test-pw"

PASTED = """\
Dana Ops · 2h ago
We need someone to build our iOS and Android app. Budget $20k, starting ASAP.
Flutter preferred.

Sam Rivera · 5h ago
I'm a Flutter developer looking for a new role. Open to work.
"""


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DASHBOARD_PASSWORD", PASSWORD)
    monkeypatch.setenv("DASHBOARD_SECRET_KEY", "test-signing-key")

    db_path = tmp_path / "web.db"
    db = Database(f"sqlite:///{db_path}")
    triage_text(db, PASTED, load_requirements(), community="flutter-devs")

    from circle_leads.web.app import create_app

    return TestClient(create_app(db_url=f"sqlite:///{db_path}"))


@pytest.fixture
def auth_client(client):
    client.post("/login", data={"password": PASSWORD})
    return client


# --- Auth -------------------------------------------------------------------


def test_app_refuses_to_start_without_a_password(monkeypatch):
    monkeypatch.delenv("DASHBOARD_PASSWORD", raising=False)
    from circle_leads.web.app import create_app

    with pytest.raises(AuthNotConfigured):
        create_app()


def test_short_password_is_rejected(monkeypatch):
    monkeypatch.setenv("DASHBOARD_PASSWORD", "abc")
    from circle_leads.web.app import create_app

    with pytest.raises(AuthNotConfigured):
        create_app()


@pytest.mark.parametrize(
    "path",
    ["/api/leads", "/api/stats", "/api/activity", "/api/communities", "/api/config"],
)
def test_api_requires_authentication(client, path):
    """The database holds other people's posts; nothing is open by default."""
    assert client.get(path).status_code == 401


def test_root_redirects_to_login_when_signed_out(client):
    assert client.get("/", follow_redirects=False).status_code == 303


def test_wrong_password_is_rejected(client):
    assert client.post("/login", data={"password": "wrong"}).status_code == 401


def test_correct_password_sets_a_session(client):
    response = client.post("/login", data={"password": PASSWORD})
    assert response.status_code == 200
    assert client.get("/api/stats").status_code == 200


def test_logout_ends_the_session(auth_client):
    auth_client.post("/logout")
    assert auth_client.get("/api/leads").status_code == 401


def test_verify_password_rejects_empty(monkeypatch):
    monkeypatch.setenv("DASHBOARD_PASSWORD", PASSWORD)
    assert verify_password(PASSWORD) is True
    assert verify_password("") is False


# --- Leads ------------------------------------------------------------------


def test_leads_endpoint_returns_triaged_leads(auth_client):
    data = auth_client.get("/api/leads").json()
    assert data["count"] >= 1
    lead = data["leads"][0]
    assert lead["classification"] == "LEAD"
    assert lead["reply_draft"]
    assert lead["review_status"] == "pending_review"


def test_leads_can_be_filtered_by_skill(auth_client):
    data = auth_client.get("/api/leads?skills=Flutter").json()
    assert all("Flutter" in lead["skills"] for lead in data["leads"])


def test_review_status_persists(auth_client):
    lead_id = auth_client.get("/api/leads").json()["leads"][0]["id"]
    response = auth_client.post(f"/api/leads/{lead_id}/status", json={"status": "contacted"})
    assert response.status_code == 200

    updated = auth_client.get("/api/leads").json()["leads"]
    assert next(x for x in updated if x["id"] == lead_id)["review_status"] == "contacted"


def test_invalid_review_status_is_rejected(auth_client):
    lead_id = auth_client.get("/api/leads").json()["leads"][0]["id"]
    assert auth_client.post(
        f"/api/leads/{lead_id}/status", json={"status": "nonsense"}
    ).status_code == 400


def test_status_on_missing_lead_is_404(auth_client):
    assert auth_client.post(
        "/api/leads/999999/status", json={"status": "contacted"}
    ).status_code == 404


# --- Triage -----------------------------------------------------------------


def test_triage_endpoint_finds_leads(auth_client):
    response = auth_client.post("/api/triage", json={
        "text": "Marco · 1h ago\nWe are hiring a React Native developer. Budget $30k.",
        "community": "mobile-founders",
    })
    data = response.json()
    assert data["total_posts"] == 1
    assert len(data["leads"]) == 1
    assert data["leads"][0]["budget"] == "Budget $30k"


def test_triage_rejects_empty_text(auth_client):
    assert auth_client.post("/api/triage", json={"text": "   "}).status_code == 400


def test_triage_requires_auth(client):
    assert client.post("/api/triage", json={"text": "hi"}).status_code == 401


# --- Stats and activity -----------------------------------------------------


def test_stats_summarize_the_database(auth_client):
    stats = auth_client.get("/api/stats").json()
    assert stats["leads"] >= 1
    assert stats["posts"] >= 2
    assert len(stats["timeline"]) == 14
    assert stats["decided_by"]


def test_activity_records_what_was_checked_and_decided(auth_client):
    events = auth_client.get("/api/activity").json()["activity"]
    kinds = {e["kind"] for e in events}
    assert "triage" in kinds
    assert "classify" in kinds

    triage = next(e for e in events if e["kind"] == "triage")
    assert triage["community"] == "flutter-devs"
    assert triage["items_seen"] == 2


def test_activity_can_be_filtered_by_kind(auth_client):
    events = auth_client.get("/api/activity?kind=classify").json()["activity"]
    assert events
    assert all(e["kind"] == "classify" for e in events)


def test_activity_never_stores_credentials(tmp_path):
    """A caller passing a token must not get it written to the log."""
    from circle_leads.storage.activity import log_activity, recent_activity

    db = Database(f"sqlite:///{tmp_path}/act.db")
    with db.session() as s:
        log_activity(
            s, kind="ingest", summary="test",
            detail={"token": "secret-abc", "access_token": "jwt-xyz", "community": "acme"},
        )
    with db.session() as s:
        detail = recent_activity(s)[0]["detail"]
    assert "token" not in detail
    assert "access_token" not in detail
    assert detail["community"] == "acme"


def test_communities_endpoint_shows_permission_state(auth_client):
    rows = auth_client.get("/api/communities").json()["communities"]
    assert rows
    # Triage must never imply an operator approved ingestion.
    assert all(c["permission_status"] != "approved" for c in rows)
