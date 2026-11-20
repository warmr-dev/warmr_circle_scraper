"""Angel Squad's feed is not Circle, and its post ids are not numbers."""

from __future__ import annotations

import tempfile

from circle_leads.scraper.angelsquad import (
    accepted_cookies, fetch_records, retarget_legacy, verify,
)
from circle_leads.storage.database import Database, get_or_create_community
from circle_leads.storage.models import Community, WatchMode, WatchState
from circle_leads.watch.poller import (
    WatchOutcome, WatchTuning, _already_seen, _check_angelsquad, _finish,
)


def test_circle_cookie_names_are_not_required_for_angel_squad():
    kept = accepted_cookies("app.angelsquad.co", [
        {"name": "session", "value": "yes"},
        {"name": "_circle_session", "value": "nope"},
    ])
    assert kept == {"session": "yes", "_circle_session": "nope"}
    assert accepted_cookies("demo.circle.so", [
        {"name": "session", "value": "ignore"},
        {"name": "_circle_session", "value": "keep"},
    ]) == {"_circle_session": "keep"}


def test_a_rejected_angel_squad_session_is_named(monkeypatch):
    class Response:
        status_code = 401
        headers = {"content-type": "application/json"}

    monkeypatch.setattr(
        "circle_leads.scraper.angelsquad.requests.get",
        lambda *a, **k: Response(),
    )
    verdict = verify({"session": "dead"})
    assert verdict["verdict"] == "rejected"
    assert "app.angelsquad.co" in verdict["detail"]


def test_the_feed_becomes_circle_shaped_records(monkeypatch):
    class Response:
        status_code = 200
        headers = {"content-type": "application/json"}

        def json(self):
            return {"items": [{
                "id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                "title": "Looking for a technical cofounder",
                "body": "We are hiring a backend engineer in Berlin.",
                "publishedAt": "2026-11-20T08:00:00Z",
                "authorId": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
                "authorName": "Avery Chen",
            }], "nextCursor": None}

    monkeypatch.setattr(
        "circle_leads.scraper.angelsquad.requests.get",
        lambda *a, **k: Response(),
    )
    status, _detail, records = fetch_records({"session": "live"})
    assert status == "valid"
    assert records[0]["name"] == "Looking for a technical cofounder"
    assert records[0]["body_plain_text"].startswith("We are hiring")
    assert records[0]["url"] == (
        "https://app.angelsquad.co/community/posts/"
        "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    )
    assert records[0]["user"]["name"] == "Avery Chen"


def test_string_ids_are_not_compared_with_the_numeric_watermark():
    assert _already_seen(3, set(), 5) is True
    assert _already_seen(8, set(), 5) is False
    assert _already_seen("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", set(), 0) is False
    assert _already_seen("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                         {"aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"}, 0) is True


def test_saving_a_session_moves_the_old_circle_row():
    db = Database("sqlite:///" + tempfile.mktemp(suffix=".db"))
    with db.session() as s:
        community = get_or_create_community(
            s, slug="angel-squad", url="https://angel-squad.circle.so",
            name="Angel Squad", platform="circle", watching=True,
        )
        s.flush()
        s.add(WatchState(community_id=community.id, host="angel-squad.circle.so",
                         mode=WatchMode.COOKIE.value, recent_ids=[]))
    retarget_legacy(db)
    with db.session() as s:
        row = s.get(Community, 1)
        assert row.host == "app.angelsquad.co"
        assert row.platform == "other"
        assert row.url == "https://app.angelsquad.co"
        watch = s.query(WatchState).one()
        assert watch.host == "app.angelsquad.co"


def test_a_string_id_does_not_break_the_watch_row(monkeypatch):
    db = Database("sqlite:///" + tempfile.mktemp(suffix=".db"))
    with db.session() as s:
        community = get_or_create_community(
            s, slug="app.angelsquad.co", url="https://app.angelsquad.co",
            platform="other", watching=True,
        )
        s.flush()
        s.add(WatchState(community_id=community.id, host="app.angelsquad.co",
                         mode=WatchMode.COOKIE.value, recent_ids=[], tier="fast"))
        s.flush()
        state = {
            "id": s.query(WatchState).one().id,
            "community_id": community.id,
            "host": "app.angelsquad.co",
            "mode": "cookie",
            "tier": "fast",
            "last_post_id": None,
            "recent_ids": [],
            "etag": None,
            "consecutive_errors": 0,
        }

    monkeypatch.setattr(
        "circle_leads.scraper.angelsquad.fetch_records",
        lambda cookies, **kw: ("valid", "listed", [{
            "id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            "name": "Hello",
            "body_plain_text": "We are hiring a backend engineer.",
            "created_at": "2026-11-20T08:00:00Z",
            "url": "https://app.angelsquad.co/community/posts/aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            "user": {"id": "1", "name": "Avery"},
        }]),
    )
    monkeypatch.setattr(
        "circle_leads.web.replay_store.load_cookies",
        lambda db, host: [{"name": "session", "value": "live"}],
    )
    outcome = _check_angelsquad(
        db, state, type("Req", (), {"excluded_content": [], "max_post_age_days": 0})(),
        tuning=WatchTuning(), use_llm=False,
    )
    assert outcome.status == "ok"
    with db.session() as s:
        row = s.query(WatchState).one()
        assert row.recent_ids == ["aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"]
        assert row.last_post_id is None


def test_finish_ignores_string_ids_for_the_numeric_watermark():
    db = Database("sqlite:///" + tempfile.mktemp(suffix=".db"))
    with db.session() as s:
        community = get_or_create_community(
            s, slug="app.angelsquad.co", url="https://app.angelsquad.co",
            platform="other",
        )
        s.flush()
        s.add(WatchState(community_id=community.id, host="app.angelsquad.co",
                         mode="cookie", recent_ids=[], last_post_id=4))
        s.flush()
        state = {"id": s.query(WatchState).one().id, "host": "app.angelsquad.co",
                 "consecutive_errors": 0}
    _finish(db, state, WatchOutcome("app.angelsquad.co", "ok"), WatchTuning(),
            seen_ids=["aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"])
    with db.session() as s:
        row = s.query(WatchState).one()
        assert row.last_post_id == 4
        assert row.recent_ids == ["aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"]
