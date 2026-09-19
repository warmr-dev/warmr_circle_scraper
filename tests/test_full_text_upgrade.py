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


# --- second review (2026-09-19) ---------------------------------------------

def _rejudge(s, post):
    """What both pipelines do for a post judged LEAD: link it to an original."""
    from circle_leads.storage.database import find_near_duplicate, live_original

    lead = s.scalar(select(Lead).where(Lead.post_id == post.id))
    dup = find_near_duplicate(s, post)
    link = dup.lead.id if dup is not None and live_original(dup.lead) else None
    if link is None and lead.duplicate_of_id not in (None, lead.id):
        if live_original(s.get(Lead, lead.duplicate_of_id)):
            link = lead.duplicate_of_id
    lead.duplicate_of_id = link
    return lead


def test_an_original_is_never_filed_as_its_reposts_duplicate(db):
    # Cookie scans read newest first: the repost is upgraded before the
    # original, and the original then found the repost as its "duplicate" --
    # two leads pointing at each other, both gone from the lead list.
    text = "We are hiring a contract Flutter developer for our delivery app, DM me. " * 3
    with db.session() as s:
        c = get_or_create_community(s, slug="acme", url="https://acme.circle.so")
        x = _post(s, c.id, "x", text, when=datetime(2026, 8, 1))
        z = _post(s, c.id, "z", text + " (reposted)", when=datetime(2026, 9, 1))
        lx = Lead(post_id=x.id, classification="LEAD", external_synced_at=datetime(2026, 8, 2))
        s.add(lx)
        s.flush()
        lz = Lead(post_id=z.id, classification="LEAD", duplicate_of_id=lx.id)
        s.add(lz)
        s.flush()
        assert _rejudge(s, z).duplicate_of_id == lx.id
        assert _rejudge(s, x).duplicate_of_id is None     # the original stays one


def test_a_repost_of_a_demoted_unsent_original_stands_on_its_own(db):
    text = "We are hiring a contract Flutter developer for our delivery app, DM me. " * 3
    with db.session() as s:
        c = get_or_create_community(s, slug="acme", url="https://acme.circle.so")
        x = _post(s, c.id, "x", text, when=datetime(2026, 8, 1))
        z = _post(s, c.id, "z", text + " (reposted)", when=datetime(2026, 9, 1))
        lx = Lead(post_id=x.id, classification="NOT_LEAD", review_status="contacted")
        s.add(lx)
        s.flush()
        s.add(Lead(post_id=z.id, classification="LEAD", duplicate_of_id=lx.id))
        s.flush()
        assert _rejudge(s, z).duplicate_of_id is None     # visible, and pushable


def test_a_repost_of_a_sent_original_stays_its_duplicate(db):
    text = "We are hiring a contract Flutter developer for our delivery app, DM me. " * 3
    with db.session() as s:
        c = get_or_create_community(s, slug="acme", url="https://acme.circle.so")
        x = _post(s, c.id, "x", text, when=datetime(2026, 8, 1))
        z = _post(s, c.id, "z", text + " (reposted)", when=datetime(2026, 9, 1))
        lx = Lead(post_id=x.id, classification="NOT_LEAD", external_synced_at=datetime(2026, 8, 2))
        s.add(lx)
        s.flush()
        s.add(Lead(post_id=z.id, classification="LEAD", duplicate_of_id=lx.id))
        s.flush()
        assert _rejudge(s, z).duplicate_of_id == lx.id    # Vini has it already


def test_angle_brackets_in_a_post_survive_the_flattening():
    from circle_leads.scraper.tiptap import tiptap_plain

    doc = {"type": "doc", "content": [
        {"type": "paragraph", "content": [
            {"type": "text", "text": "Need a Flutter dev with <5 years is fine, budget $6k/month."}]},
        {"type": "paragraph", "content": [
            {"type": "text", "text": "Apply -> DM me with your portfolio."}]}]}
    assert tiptap_plain(doc) == ("Need a Flutter dev with <5 years is fine, budget "
                                 "$6k/month. Apply -> DM me with your portfolio.")


def test_a_shared_session_keeps_the_blocks_before_a_failure(tmp_path):
    # One connection for a space's writes, but a transaction per block: a
    # dropped connection mid-space must not take the earlier posts with it
    # (awithub, 2026-09-19, after an LLM call per post made spaces slow).
    from circle_leads.storage.models import Community

    d = Database(f"sqlite:///{tmp_path}/shared.db")
    with pytest.raises(RuntimeError):
        with d.shared_session():
            with d.session() as s:
                get_or_create_community(s, slug="kept", url="https://kept.circle.so")
            with d.session() as s:
                get_or_create_community(s, slug="lost", url="https://lost.circle.so")
                raise RuntimeError("connection dropped")
    with d.session() as s:
        slugs = set(s.scalars(select(Community.slug)).all())
    assert slugs == {"kept"}


class _DownBackend:
    """An LLM that is configured but answers nothing (outage, spent credits)."""

    model = "down"

    def complete(self, system, user):
        raise RuntimeError("402: out of credits")


def _outage(monkeypatch):
    import circle_leads.triage.pipeline as tp

    from circle_leads.export.vini_ingest import PushResult

    pushed = []

    def fake_push(_session, ids, **_kw):
        pushed.extend(ids)
        return PushResult()

    monkeypatch.setattr(tp, "push_leads_by_ids", fake_push)
    monkeypatch.setattr(tp, "make_backend", lambda: _DownBackend())
    return tp, pushed


def test_an_llm_outage_holds_a_rules_lead_instead_of_pushing_it(db, dev_requirements, monkeypatch):
    # OpenRouter credits ran low on 2026-09-19: with the model silent, the
    # rules alone would have pushed their leads -- two thirds of which the
    # model had rejected the night before -- straight to Vini.
    tp, pushed = _outage(monkeypatch)
    tp.triage_records(
        db, [{"content": FULL, "published_at": WHEN, "author": {"display_name": "Dana"}}],
        dev_requirements, community="acme", use_llm=True)
    with db.session() as s:
        lead = s.scalar(select(Lead))
        assert lead is not None and lead.classification == "LEAD"
        assert lead.decided_by == "rules"
        assert "Held for review" in lead.reason
        assert lead.external_synced_at is None
    assert pushed == []


def test_an_llm_outage_does_not_retire_an_earlier_verdict(db, dev_requirements, monkeypatch):
    tp, pushed = _outage(monkeypatch)
    vague = ("Anyone around here tried a few different tools for our project "
             "planning? Curious what works for small teams, happy to compare notes.")
    with db.session() as s:
        c = get_or_create_community(s, slug="acme", url="https://acme.circle.so")
        y = _post(s, c.id, "triage:old", vague[:120] + "…")
        y.classified = True
        s.add(Lead(post_id=y.id, classification="LEAD", decided_by="llm",
                   reason="judged by the model earlier"))
    tp.triage_records(
        db, [{"content": vague, "published_at": WHEN, "author": {"display_name": "Dana"}}],
        dev_requirements, community="acme", use_llm=True)
    with db.session() as s:
        assert s.scalar(select(func.count()).select_from(Post)) == 1   # upgraded, re-judged
        lead = s.scalar(select(Lead))
        assert lead is not None
        assert (lead.classification, lead.reason) == ("LEAD", "judged by the model earlier")
    assert pushed == []
