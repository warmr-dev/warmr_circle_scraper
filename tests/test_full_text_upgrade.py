"""Re-reading stored previews in full must not lose, double or re-send leads.

Every post stored before 2026-09-18 is Circle's ~255-char preview. These cover
what an independent review of the full-text change found (2026-09-19): a lead
with duplicates pointing at it could not be deleted (leads.duplicate_of_id has
no ON DELETE), leads were deleted silently, a former duplicate was pushed to
Vini again, and the three readers turned one post into different strings.
"""

from datetime import datetime

import pytest
from sqlalchemy import event, func, select

from circle_leads.storage.database import (
    Database,
    get_or_create_author,
    get_or_create_community,
    retire_lead,
    upsert_post,
)
from circle_leads.storage.models import Lead, Post

WHEN = datetime(2026, 9, 1, 10, 0, 0, 123000)
PREVIEW = ("We are hiring a Flutter developer to build our delivery app, "
           "contract, remote, start next month. DM me with your portfolio "
           "and your rate so we can talk this week about the details of…")
FULL = PREVIEW[:-1] + " the project and the timeline. Budget is flexible."


@pytest.fixture
def db(tmp_path):
    d = Database(f"sqlite:///{tmp_path}/fk.db")
    # SQLite ignores foreign keys unless asked; Postgres enforces them. Without
    # this the duplicate_of_id failure is invisible to the suite.
    event.listen(d.engine, "connect",
                 lambda conn, _rec: conn.execute("PRAGMA foreign_keys=ON"))
    d.engine.dispose()
    return d


def _post(s, community_id, sid, content, when=WHEN):
    post, _ = upsert_post(s, community_id=community_id, record={
        "source_content_id": sid, "content": content, "published_at": when})
    return post


def test_a_lead_other_leads_duplicate_is_demoted_not_deleted(db):
    with db.session() as s:
        c = get_or_create_community(s, slug="acme", url="https://acme.circle.so")
        x = _post(s, c.id, "x", "Original hiring post " * 5)
        y = _post(s, c.id, "y", "Copy of the hiring post " * 5, when=datetime(2026, 9, 2))
        lx = Lead(post_id=x.id, classification="LEAD")
        s.add(lx)
        s.flush()
        s.add(Lead(post_id=y.id, classification="LEAD", duplicate_of_id=lx.id))
        s.flush()
        outcome = retire_lead(s, lx, reason="an article")
        assert outcome == "demoted"
    with db.session() as s:   # the commit went through: no FK violation
        lx = s.scalar(select(Lead).where(Lead.duplicate_of_id.is_(None)))
        assert lx.classification == "NOT_LEAD"
        assert "duplicates" in lx.reason


def test_a_reviewed_lead_is_demoted_and_an_untouched_one_deleted(db):
    with db.session() as s:
        c = get_or_create_community(s, slug="acme", url="https://acme.circle.so")
        a = _post(s, c.id, "a", "First hiring post " * 5)
        b = _post(s, c.id, "b", "Second hiring post " * 5, when=datetime(2026, 9, 2))
        reviewed = Lead(post_id=a.id, classification="LEAD", review_status="contacted")
        untouched = Lead(post_id=b.id, classification="LEAD")
        s.add_all([reviewed, untouched])
        s.flush()
        assert retire_lead(s, reviewed, reason="x") == "demoted"
        assert retire_lead(s, untouched, reason="x") == "deleted"
    with db.session() as s:
        assert s.scalar(select(func.count()).select_from(Lead)) == 1


def test_a_former_duplicate_is_not_pushed_again_after_its_full_text(db, dev_requirements, monkeypatch):
    import circle_leads.triage.pipeline as tp

    from circle_leads.export.vini_ingest import PushResult

    pushed = []

    def fake_push(_session, ids, **_kw):
        pushed.extend(ids)
        return PushResult()

    monkeypatch.setattr(tp, "push_leads_by_ids", fake_push)
    with db.session() as s:
        c = get_or_create_community(s, slug="acme", url="https://acme.circle.so")
        x = _post(s, c.id, "x", "Other text that was the original " * 4,
                  when=datetime(2026, 8, 1))
        y = _post(s, c.id, "triage:old", PREVIEW)
        lx = Lead(post_id=x.id, classification="LEAD", external_synced_at=datetime(2026, 8, 2))
        s.add(lx)
        s.flush()
        s.add(Lead(post_id=y.id, classification="LEAD", duplicate_of_id=lx.id))
        y.classified = True
        lx_id = lx.id

    res = tp.triage_records(
        db, [{"content": FULL, "published_at": WHEN, "author": {"display_name": "Dana"}}],
        dev_requirements, community="acme")
    assert res.total_posts == 1
    with db.session() as s:
        ly = s.scalar(select(Lead).where(Lead.duplicate_of_id.is_not(None)))
        assert ly is not None and ly.duplicate_of_id == lx_id
        assert s.scalar(select(func.count()).select_from(Post)) == 2   # upgraded in place
    assert pushed == []


def test_all_readers_turn_one_post_into_the_same_text():
    # Two readers used to join TipTap nodes differently ("Hiring : ..." vs
    # "Hiring: ..."), so one post read by the harvest and by a cookie scan
    # became two rows.
    from circle_leads.scraper import member_api_reader, member_feed, public_reader

    record = {
        "name": "Hiring",
        "truncated_content": "Hiring: ping @Jane Doe.",
        "tiptap_body": {"body": {"type": "doc", "content": [
            {"type": "paragraph", "content": [
                {"type": "text", "text": "Hiring", "marks": [{"type": "bold"}]},
                {"type": "text", "text": ": a contract Flutter developer for our delivery app, ping "},
                {"type": "mention", "attrs": {"sgid": "x"}, "circle_ios_fallback_text": "@Jane Doe"},
                {"type": "text", "text": "."}]},
            {"type": "bulletList", "content": [
                {"type": "listItem", "content": [{"type": "paragraph", "content": [
                    {"type": "text", "text": "Remote"}]}]}]},
        ]}},
    }
    bodies = {m.__name__: m._extract_text(record)[1]
              for m in (public_reader, member_feed, member_api_reader)}
    assert len(set(bodies.values())) == 1, bodies
    body = next(iter(bodies.values()))
    assert "Hiring: a contract Flutter developer" in body
    assert "@Jane Doe." in body and "Remote" in body


def test_whitespace_only_difference_keeps_the_verdict(db):
    spaced = "Hiring : a contract Flutter developer , ping @Jane Doe . Remote " * 2
    tight = "Hiring: a contract Flutter developer, ping @Jane Doe. Remote " * 2
    with db.session() as s:
        c = get_or_create_community(s, slug="acme", url="https://acme.circle.so")
        old = _post(s, c.id, "triage:spaced", spaced)
        old.classified = True
        cid, pid = c.id, old.id
    with db.session() as s:
        post, outcome = upsert_post(s, community_id=cid, record={
            "source_content_id": "triage:tight", "content": tight, "published_at": WHEN})
        assert outcome == "unchanged" and post.id == pid
        assert post.classified is True           # nothing new to classify
        assert post.source_content_id == "triage:tight"


def test_two_rows_at_one_timestamp_match_the_right_one(db):
    with db.session() as s:
        c = get_or_create_community(s, slug="acme", url="https://acme.circle.so")
        other = _post(s, c.id, "other", "A completely different announcement " * 4)
        mine = _post(s, c.id, "mine", PREVIEW)
        post, outcome = upsert_post(s, community_id=c.id, record={
            "source_content_id": "full", "content": FULL, "published_at": WHEN})
        assert outcome == "updated"
        assert post.id == mine.id and post.id != other.id


def test_same_name_with_a_different_profile_is_another_author(db):
    with db.session() as s:
        c = get_or_create_community(s, slug="acme", url="https://acme.circle.so")
        a = get_or_create_author(s, community_id=c.id, source_author_id=None,
                                 display_name="Alex", profile_url="https://x/u/1")
        b = get_or_create_author(s, community_id=c.id, source_author_id=None,
                                 display_name="Alex", profile_url="https://x/u/2")
        again = get_or_create_author(s, community_id=c.id, source_author_id=None,
                                     display_name="Alex", profile_url="https://x/u/1")
        assert a.id != b.id and again.id == a.id
