"""Tests for persisting search finds and flagging new ones."""

import pytest

from circle_leads.discovery.finder import RankedCommunity
from circle_leads.discovery.persist import new_since, persist_finds
from circle_leads.storage.database import Database


@pytest.fixture
def db(tmp_path):
    return Database(f"sqlite:///{tmp_path}/persist.db")


def rc(slug, score, name=None, url=None, free=False):
    return RankedCommunity(
        slug=slug, name=name or slug, join_url=url or f"https://{slug}.circle.so",
        score=score, price_label="Free" if free else None, is_free=free,
        reasons=["founder"],
    )


def test_first_run_flags_everything_new(db):
    result = persist_finds(db, [rc("a", 60), rc("b", 40)], niche="founders", min_score=25)
    assert result.new_count == 2
    assert {c.slug for c in result.new} == {"a", "b"}


def test_second_run_flags_nothing_new(db):
    finds = [rc("a", 60), rc("b", 40)]
    persist_finds(db, finds, niche="founders", min_score=25)
    second = persist_finds(db, finds, niche="founders", min_score=25)
    assert second.new_count == 0
    assert second.unchanged == 2


def test_only_genuinely_new_slugs_are_flagged(db):
    persist_finds(db, [rc("a", 60)], niche="founders", min_score=25)
    second = persist_finds(db, [rc("a", 60), rc("c", 50)], niche="founders", min_score=25)
    assert {x.slug for x in second.new} == {"c"}


def test_min_score_filters_finds(db):
    result = persist_finds(db, [rc("a", 60), rc("low", 10)], min_score=25)
    assert result.new_count == 1
    assert new_since(db) and all(r["slug"] != "low" for r in new_since(db))


def test_improved_score_updates_but_does_not_reflag(db):
    persist_finds(db, [rc("a", 40)], min_score=25)
    second = persist_finds(db, [rc("a", 70)], min_score=25)
    assert second.new_count == 0
    assert len(second.updated) == 1
    assert new_since(db)[0]["score"] == 70


def test_new_since_lists_unvisited_best_first(db):
    persist_finds(db, [rc("a", 40), rc("b", 80), rc("c", 60)], min_score=25)
    rows = new_since(db)
    assert [r["slug"] for r in rows] == ["b", "c", "a"]


def test_find_never_implies_approval(db):
    """A discovered community stays a candidate, not approved for ingestion."""
    from sqlalchemy import select

    from circle_leads.storage.models import Community

    persist_finds(db, [rc("a", 60)], min_score=25)
    with db.session() as s:
        c = s.scalar(select(Community).where(Community.slug == "a"))
        assert c.permission_status == "candidate"
        assert c.access_status == "not_visited"
        assert c.is_ingestable is False


def test_persist_logs_activity(db):
    from circle_leads.storage.activity import recent_activity

    persist_finds(db, [rc("a", 60)], niche="flutter", min_score=25)
    with db.session() as s:
        events = recent_activity(s, kind="discover")
    assert events
    assert "flutter" in events[0]["summary"]
