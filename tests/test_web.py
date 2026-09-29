"""The dashboard's gate and the routes other clients call: sign-in, the
extension's session route scope, the tick pinger, the activity log.

The six sections have their own tests (tests/test_dash_*.py)."""

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from pathlib import Path  # noqa: E402

from circle_leads.config.settings import load_requirements as _load_requirements  # noqa: E402
from circle_leads.storage.database import Database  # noqa: E402
from circle_leads.triage.pipeline import triage_text  # noqa: E402
from circle_leads.web.auth import AuthNotConfigured, verify_password  # noqa: E402

# Stable developer-targeting test config (not the live product config, which is
# now retargeted to founders/CEOs). The web app is built with this via
# create_app(config_path=...), and seed data is triaged with it, so these tests
# assert dashboard behaviour on developer-hiring leads regardless of the live aim.
_DEV_CONFIG = str(Path(__file__).parent / "fixtures" / "dev_requirements.yaml")


def load_requirements(config_path=None):
    return _load_requirements(config_path or _DEV_CONFIG)

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

    # Point the app at a PER-TEST COPY of the dev config, not the shared fixture
    # file: the /api/config POST writes to its config_path, so a save-test must
    # not corrupt the fixture other tests load.
    import shutil
    test_cfg = tmp_path / "requirements.yaml"
    shutil.copy(_DEV_CONFIG, test_cfg)
    return TestClient(create_app(db_url=f"sqlite:///{db_path}", config_path=str(test_cfg)))


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
    ["/api/dash/analytics", "/api/dash/monitoring", "/api/dash/leads",
     "/api/dash/communities", "/api/dash/attention", "/api/dash/activity",
     "/api/dash/runtime", "/api/dash/schedule", "/api/dash/config",
     "/api/connections"],
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
    assert client.get("/api/dash/analytics").status_code == 200


def test_logout_ends_the_session(auth_client):
    auth_client.post("/logout")
    assert auth_client.get("/api/dash/leads").status_code == 401


def test_verify_password_rejects_empty(monkeypatch):
    monkeypatch.setenv("DASHBOARD_PASSWORD", PASSWORD)
    assert verify_password(PASSWORD) is True
    assert verify_password("") is False


# --- Activity log -------------------------------------------------------------


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


# --- /api/tick (free-tier external scheduler) -------------------------------


def test_tick_requires_auth_or_token(client):
    assert client.get("/api/tick").status_code == 401


def test_tick_accepts_token(client, monkeypatch):
    monkeypatch.setenv("TICK_TOKEN", "sekret")
    # rebuild the app so it picks up the token
    from circle_leads.web.app import create_app
    from fastapi.testclient import TestClient
    import tempfile
    c = TestClient(create_app(db_url=f"sqlite:///{tempfile.mktemp()}.db"))
    r = c.get("/api/tick?token=sekret")
    assert r.status_code == 200
    assert r.json().get("ran") is True  # default schedule, never run -> due


def test_tick_wrong_token_rejected(client, monkeypatch):
    monkeypatch.setenv("TICK_TOKEN", "sekret")
    from circle_leads.web.app import create_app
    from fastapi.testclient import TestClient
    import tempfile
    c = TestClient(create_app(db_url=f"sqlite:///{tempfile.mktemp()}.db"))
    assert c.get("/api/tick?token=wrong").status_code == 401


def test_tick_skips_when_not_due(client, monkeypatch):
    monkeypatch.setenv("TICK_TOKEN", "sekret")
    from circle_leads.web.app import create_app
    from fastapi.testclient import TestClient
    import tempfile
    c = TestClient(create_app(db_url=f"sqlite:///{tempfile.mktemp()}.db"))
    c.get("/api/tick?token=sekret")            # first run marks it done
    import time; time.sleep(0.2)
    r = c.get("/api/tick?token=sekret")        # immediately after -> not due
    assert r.json()["ran"] is False
