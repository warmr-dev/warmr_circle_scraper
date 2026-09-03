"""Tests for the harvest orchestrator (search -> read public -> classify)."""

import pytest

from circle_leads.config.settings import load_requirements
from circle_leads.harvest import harvest
from circle_leads.storage.database import Database, get_or_create_community


@pytest.fixture
def reqs():
    return load_requirements()


@pytest.fixture
def db(tmp_path):
    d = Database(f"sqlite:///{tmp_path}/harvest.db")
    with d.session() as s:
        c = get_or_create_community(
            s, slug="pub", url="https://pub.circle.so"
        )
        c.relevance_score = 50
    return d


class FakeSpace:
    def __init__(self, sid, name, is_public=None):
        self.id = sid
        self.slug = name.lower().replace(" ", "-")
        self.name = name
        self.is_public = is_public


class FakePublicReader:
    """Stub reader: one public "Job Posts" space with two posts."""

    def __init__(self, host, **kw):
        self.community_host = host
        self.base = f"https://{host}"

    def list_spaces(self):
        return [FakeSpace("1", "Job Posts")]

    def read_space(self, sid, **kw):
        return True, [
            {"id": 10, "name": "Hiring",
             "truncated_content": "We are hiring a software engineer",
             "created_at": "2026-08-30T10:00:00Z"},
            {"id": 11, "name": "",
             "truncated_content": "I'm a dev looking for a job, open to work"},
        ]


def test_harvest_reads_public_community(db, reqs, monkeypatch):
    import circle_leads.harvest as h

    monkeypatch.setattr(h, "PublicReader", FakePublicReader)
    result = harvest(db, reqs, search=False)
    assert result.communities_read == 1
    assert result.public_spaces == 1
    assert result.posts_read == 2
    assert result.leads_found == 1  # the hiring post, not the job seeker


def test_harvest_marks_communities_synced(db, reqs, monkeypatch):
    import circle_leads.harvest as h
    from sqlalchemy import select
    from circle_leads.storage.models import Community

    class EmptyReader:
        def __init__(self, host, **kw):
            self.community_host = host; self.base = f"https://{host}"
        def list_spaces(self): return []
        def read_space(self, sid, **kw): return False, []
    monkeypatch.setattr(h, "PublicReader", EmptyReader)
    harvest(db, reqs, search=False)
    with db.session() as s:
        c = s.scalar(select(Community).where(Community.slug == "pub"))
        assert c.last_synced_at is not None


def test_harvest_only_new_skips_synced(db, reqs, monkeypatch):
    import circle_leads.harvest as h
    from datetime import datetime

    calls = []
    class CountingReader:
        def __init__(self, host, **kw):
            self.community_host = host; self.base = f"https://{host}"
        def list_spaces(self): calls.append(1); return []
        def read_space(self, sid, **kw): return False, []
    monkeypatch.setattr(h, "PublicReader", CountingReader)

    harvest(db, reqs, search=False)          # reads + marks synced
    calls.clear()
    harvest(db, reqs, search=False, only_new=True)  # should skip
    assert calls == []


def test_harvest_search_persists_new_communities(db, reqs, monkeypatch):
    import circle_leads.harvest as h

    class FakeDisc:
        backend = "stub"
        ranked = []

    class EmptyReader:
        def __init__(self, host, **kw):
            self.community_host = host; self.base = f"https://{host}"
        def list_spaces(self): return []
        def read_space(self, sid, **kw): return False, []
    monkeypatch.setattr(h, "discover_by_search", lambda *a, **k: FakeDisc())
    monkeypatch.setattr(h, "PublicReader", EmptyReader)
    result = harvest(db, reqs, niches=["x"], search=True)
    assert isinstance(result.new_communities, int)


def test_harvest_skips_non_subdomain_communities(db, reqs, monkeypatch):
    """Discover /products listings can't be read; only subdomains are."""
    import circle_leads.harvest as h

    with db.session() as s:
        c = get_or_create_community(
            s, slug="disc", url="https://discover.circle.so/products/x"
        )
        c.relevance_score = 90

    read = []
    class RecordingReader:
        def __init__(self, host, **kw):
            self.community_host = host; self.base = f"https://{host}"; read.append(host)
        def list_spaces(self): return []
        def read_space(self, sid, **kw): return False, []
    monkeypatch.setattr(h, "PublicReader", RecordingReader)
    harvest(db, reqs, search=False)
    assert "discover.circle.so" not in " ".join(read)
