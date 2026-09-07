"""Add-community-by-URL endpoint: probe public API, save, report readability.

Never authenticates as the user; a members-only community is saved with a
pointer to the local browser sign-in flow, not an automated login.
"""
from __future__ import annotations

import tempfile

import pytest
from fastapi.testclient import TestClient

from circle_leads.web.app import create_app
from circle_leads.storage.database import Database
from circle_leads.storage.models import Community
from sqlalchemy import select


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("DASHBOARD_PASSWORD", "testpassword")
    monkeypatch.setenv("DASHBOARD_SECRET_KEY", "k")
    db_url = "sqlite:///" + tempfile.mktemp(suffix=".db")
    app = create_app(db_url=db_url)
    c = TestClient(app)
    c.post("/login", data={"password": "testpassword"})
    c._db_url = db_url
    return c


def _stub_reader(monkeypatch, *, spaces, readable, name):
    """Make PublicReader return controlled results (no network)."""
    import circle_leads.web.app as appmod  # endpoint imports PublicReader lazily
    from circle_leads.scraper import public_reader as pr

    class _Sp:
        def __init__(self, i): self.id = i; self.slug = f"s{i}"; self.name = f"S{i}"

    class _Stub:
        def __init__(self, host, **k): self.host = host
        def list_spaces(self): return [_Sp(i) for i in range(spaces)]
        def community_name(self): return name
        def read_space(self, sid, **k): return (readable, [{"id": 1}] if readable else [])

    monkeypatch.setattr(pr, "PublicReader", _Stub)


def test_add_readable_community(client, monkeypatch):
    _stub_reader(monkeypatch, spaces=5, readable=True, name="Altea")
    r = client.post("/api/communities/add", json={"url": "https://altea.circle.so"})
    assert r.status_code == 200
    d = r.json()
    assert d["readable"] is True and d["name"] == "Altea"
    db = Database(client._db_url)
    with db.session() as s:
        assert s.scalar(select(Community).where(Community.slug == "altea")) is not None


def test_add_members_only_points_to_local_login(client, monkeypatch):
    _stub_reader(monkeypatch, spaces=8, readable=False, name=None)
    r = client.post("/api/communities/add", json={"url": "https://saas-stars.circle.so"})
    assert r.status_code == 200
    d = r.json()
    assert d["readable"] is False
    assert "read-feed" in d["note"]  # points to local sign-in, not auto-login


def test_add_rejects_non_community_hosts(client):
    for host in ("https://login.circle.so", "https://discover.circle.so"):
        assert client.post("/api/communities/add", json={"url": host}).status_code == 400


def test_add_requires_a_url(client):
    assert client.post("/api/communities/add", json={}).status_code == 400
