"""Config saving must work on a read-only filesystem (serverless deploys).

The packaged requirements.yaml lives under the install dir, which is read-only
on Lambda/serverless (/var/task -> Errno 30). Dashboard config edits therefore
persist in the DB, not the file. These tests pin that behaviour.
"""

from __future__ import annotations

import os
import stat
import tempfile

import pytest
from fastapi.testclient import TestClient

from circle_leads.config.settings import DEFAULT_CONFIG_PATH
from circle_leads.storage.database import Database
from circle_leads.storage.settings_store import (
    get_requirements_override, set_requirements_override,
)


@pytest.fixture
def app_client(monkeypatch):
    monkeypatch.setenv("DASHBOARD_PASSWORD", "testpassword")
    monkeypatch.setenv("DASHBOARD_SECRET_KEY", "k")
    from circle_leads.web.app import create_app
    db_url = "sqlite:///" + tempfile.mktemp(suffix=".db")

    def make():
        c = TestClient(create_app(db_url=db_url))
        c.post("/login", data={"password": "testpassword"})
        return c

    return make


@pytest.fixture
def readonly_config():
    """Make the packaged config read-only for the duration of a test."""
    orig = os.stat(DEFAULT_CONFIG_PATH).st_mode
    os.chmod(DEFAULT_CONFIG_PATH, stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
    try:
        yield
    finally:
        os.chmod(DEFAULT_CONFIG_PATH, orig)


def test_saving_config_works_when_the_file_is_read_only(app_client, readonly_config):
    c = app_client()
    cfg = c.get("/api/config").json()
    cfg["target_roles"] = ["Backend Developer", "C++ Engineer"]
    r = c.post("/api/config", json=cfg)
    assert r.status_code == 200, r.text  # used to be Errno 30
    assert r.json()["config"]["target_roles"] == ["Backend Developer", "C++ Engineer"]


def test_saved_config_survives_a_restart(app_client, readonly_config):
    c = app_client()
    cfg = c.get("/api/config").json()
    cfg["minimum_confidence"] = 0.77
    assert c.post("/api/config", json=cfg).status_code == 200

    # A fresh app instance on the same DB must read the override back.
    c2 = app_client()
    assert c2.get("/api/config").json()["minimum_confidence"] == 0.77


def test_invalid_config_is_rejected_without_writing(app_client):
    c = app_client()
    cfg = c.get("/api/config").json()
    cfg["minimum_confidence"] = "not a number"
    r = c.post("/api/config", json=cfg)
    assert r.status_code == 400
    assert "Invalid config" in r.json()["detail"]


def test_override_round_trips_in_the_store():
    db = Database("sqlite:///" + tempfile.mktemp(suffix=".db"))
    assert get_requirements_override(db) is None
    set_requirements_override(db, {"target_roles": ["X"], "minimum_confidence": 0.5})
    assert get_requirements_override(db)["target_roles"] == ["X"]


def test_locked_safety_fields_are_not_overwritten(app_client, readonly_config):
    """excluded_content and rate_limit stay server-controlled even via save."""
    c = app_client()
    cfg = c.get("/api/config").json()
    before = cfg["excluded_content"]
    cfg["excluded_content"] = []            # attempt to clear the safety allowlist
    cfg["target_roles"] = ["Engineer"]
    r = c.post("/api/config", json=cfg)
    assert r.status_code == 200
    assert r.json()["config"]["excluded_content"] == before  # unchanged
