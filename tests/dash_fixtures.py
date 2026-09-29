"""Builders shared by the dashboard section tests (tests/test_dash_*.py).

A real app on a fresh SQLite database, signed in, with the clock pinned, and
small helpers that write exactly the rows a test is about.
"""

from __future__ import annotations

import itertools
import shutil
from datetime import datetime
from pathlib import Path

from fastapi.testclient import TestClient

from circle_leads.storage.database import Database, get_or_create_community
from circle_leads.storage.models import (
    Author, CircleConnection, Community, JoinStatus, Lead, Post, ReplaySession, Setting,
    WatchState,
)

PASSWORD = "dashboard-test-pw"
DEV_CONFIG = Path(__file__).parent / "fixtures" / "dev_requirements.yaml"
# 2026-09-29 11:00 UTC = 18:00 at UTC+7, so "today" at UTC+7 began at
# 2026-09-28 17:00 UTC and at UTC at 2026-09-29 00:00.
NOW = datetime(2026, 9, 29, 11, 0, 0)
TZ_PLUS_7 = -420

_ids = itertools.count(1)


def make_app(tmp_path, monkeypatch, *, now: datetime | None = NOW):
    """(client, db, app): a signed-in client on a fresh database."""
    from circle_leads.web.app import create_app
    from circle_leads.web.sections.deps import utc_now

    monkeypatch.setenv("DASHBOARD_PASSWORD", PASSWORD)
    monkeypatch.setenv("DASHBOARD_SECRET_KEY", "test-signing-key")
    url = f"sqlite:///{tmp_path / 'dash.db'}"
    db = Database(url)
    config = tmp_path / "requirements.yaml"
    shutil.copy(DEV_CONFIG, config)
    app = create_app(db_url=url, config_path=str(config))
    if now is not None:
        app.dependency_overrides[utc_now] = lambda: now
    client = TestClient(app)
    assert client.post("/login", data={"password": PASSWORD}).status_code == 200
    return client, db, app


def community(s, slug: str, *, url: str | None = None, **fields) -> Community:
    c = get_or_create_community(s, slug=slug, url=url or f"https://{slug}.circle.so")
    fields.setdefault("platform", "circle")
    fields.setdefault("join_status", JoinStatus.NOT_ATTEMPTED.value)
    for key, value in fields.items():
        setattr(c, key, value)
    s.flush()
    return c


def lead(s, c: Community, *, created_at: datetime = NOW, classification: str = "LEAD",
         author: bool = True, content: str = "We need a developer to build our app.",
         **fields) -> Lead:
    n = next(_ids)
    author_row = None
    if author:
        author_row = Author(community_id=c.id, source_author_id=f"a{n}", display_name=f"Author {n}")
        s.add(author_row)
        s.flush()
    post = Post(community_id=c.id, author_id=author_row.id if author_row else None,
                source_content_id=f"post-{n}", content_type="post", content=content,
                url=f"{c.url}/c/jobs/{n}", published_at=created_at, dedup_hash=f"h{n}",
                classified=True, scraped_at=created_at)
    s.add(post)
    s.flush()
    row = Lead(post_id=post.id, classification=classification, confidence=0.9,
               lead_score=70, priority="MEDIUM", created_at=created_at, **fields)
    s.add(row)
    s.flush()
    return row


def session_for(s, host: str, *, created_at: datetime = NOW, plaintext: bool = False) -> None:
    s.add(ReplaySession(host=host, encrypted_cookies=("plain:" if plaintext else "enc:") + "x",
                        cookie_count=2, created_at=created_at))
    s.flush()


def watch(s, c: Community, **fields) -> WatchState:
    fields.setdefault("mode", "anon")
    fields.setdefault("tier", "fast")
    fields.setdefault("next_check_at", NOW)
    row = WatchState(community_id=c.id, host=c.host, recent_ids=[], **fields)
    s.add(row)
    s.flush()
    return row


def connection(s, host: str, state: str, detail: str | None = None, **fields) -> CircleConnection:
    row = CircleConnection(host=host, state=state, state_detail=detail, **fields)
    s.add(row)
    s.flush()
    return row


def setting(s, key: str, value: str) -> None:
    s.merge(Setting(key=key, value=value))
    s.flush()
