"""Community watchlist ("subscriptions"): watched communities are polled first."""

from __future__ import annotations

import tempfile

from circle_leads.storage.database import Database
from circle_leads.storage.models import Community, AccessState, PermissionStatus
from circle_leads.harvest import _community_hosts


def _mk(db, slug, score, watching):
    with db.session() as s:
        c = Community(
            slug=slug, name=slug, url=f"https://{slug}.circle.so",
            discovery_source="t", access_status=AccessState.NOT_VISITED.value,
            permission_status=PermissionStatus.CANDIDATE.value,
            relevance_score=score, relevant=True,
        )
        c.watching = watching
        s.add(c)


def _db():
    return Database("sqlite:///" + tempfile.mktemp(suffix=".db"))


def test_watched_community_polled_before_higher_scoring_unwatched():
    db = _db()
    _mk(db, "big", 90, False)
    _mk(db, "watched", 10, True)
    order = [slug for _, slug, _, _ in _community_hosts(db, limit=10)]
    assert order[0] == "watched"  # watched first despite lower score


def test_watched_only_filters_to_the_watchlist():
    db = _db()
    _mk(db, "big", 90, False)
    _mk(db, "watched", 10, True)
    slugs = [slug for _, slug, _, _ in _community_hosts(db, limit=10, watched_only=True)]
    assert slugs == ["watched"]


def test_hosts_tuple_carries_watching_flag():
    db = _db()
    _mk(db, "w", 50, True)
    host, slug, last_synced, watching = _community_hosts(db, limit=1)[0]
    assert watching is True


def test_watching_defaults_false():
    db = _db()
    _mk(db, "u", 50, False)
    _, _, _, watching = _community_hosts(db, limit=1)[0]
    assert watching is False


def test_migration_adds_watching_column_to_old_db():
    """An existing DB missing the column gets it added on open."""
    import sqlite3
    from sqlalchemy import inspect
    path = tempfile.mktemp(suffix=".db")
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE communities (id INTEGER PRIMARY KEY, slug TEXT UNIQUE, "
        "url TEXT UNIQUE, name TEXT)"
    )
    con.commit()
    con.close()
    db = Database(f"sqlite:///{path}")
    cols = {c["name"] for c in inspect(db.engine).get_columns("communities")}
    assert "watching" in cols
