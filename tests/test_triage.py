"""Tests for manual triage: splitting pasted text, drafting replies, and the
end-to-end path from a paste to a ranked lead list.
"""

import pytest

from circle_leads.config.settings import load_requirements
from circle_leads.storage.database import Database
from circle_leads.triage.pipeline import triage_text
from circle_leads.triage.reply import draft_reply
from circle_leads.triage.splitter import split_posts

PASTED = """\
Dana Ops · 2h ago
We're a seed-stage startup and we need someone to build our iOS and Android app.
Budget is around $20k for the first milestone, and we'd like to start ASAP.
Flutter preferred. Remote is fine.
12 likes  4 comments

Sam Rivera · 5h ago
Hi everyone! I'm a Flutter developer with 5 years experience looking for a new role.
Open to work, remote preferred. My portfolio is in my profile.
3 likes

Priya N · yesterday
Anyone know a good Flutter dev? We're rebuilding our booking app and our
current contractor just dropped out.
Reply

Jordan Blake · Sep 1
Flutter 3.3 is out and the new rendering pipeline is genuinely faster.
28 likes  9 comments
"""


@pytest.fixture(scope="module")
def reqs():
    return load_requirements()


@pytest.fixture
def db(tmp_path):
    return Database(f"sqlite:///{tmp_path}/triage.db")


# --- Splitting --------------------------------------------------------------


def test_splits_on_bylines():
    posts = split_posts(PASTED)
    assert len(posts) == 4
    assert [p.author for p in posts] == ["Dana Ops", "Sam Rivera", "Priya N", "Jordan Blake"]
    assert posts[0].posted_label == "2h ago"
    assert posts[2].posted_label == "yesterday"


def test_engagement_chrome_is_stripped():
    """"12 likes  4 comments" is UI furniture, not what the person wrote."""
    posts = split_posts(PASTED)
    for p in posts:
        assert "likes" not in p.content
        assert "comments" not in p.content
    assert posts[0].content.endswith("Remote is fine.")


def test_splits_on_explicit_separator():
    text = "First post about hiring a dev.\n---\nSecond post about something else."
    posts = split_posts(text)
    assert len(posts) == 2
    assert "First post" in posts[0].content


def test_splits_on_blank_lines_without_bylines():
    text = "We are hiring a Flutter developer.\n\nCompletely separate second post here."
    assert len(split_posts(text)) == 2


def test_single_post_without_structure():
    posts = split_posts("We are looking for a Flutter developer to build our app.")
    assert len(posts) == 1
    assert posts[0].author is None


@pytest.mark.parametrize("text", ["", "   \n  ", "short"])
def test_empty_or_tiny_input_yields_nothing(text):
    assert split_posts(text) == []


def test_crlf_input_is_handled():
    assert len(split_posts("A post about hiring devs.\r\n\r\nAnother separate post.")) == 2


# --- Reply drafting ---------------------------------------------------------


def test_draft_names_the_role_and_asks_a_question():
    draft = draft_reply({
        "author": "Priya N", "job_title": "Flutter Developer",
        "skills": ["Flutter"], "urgency": "High",
    })
    assert "Priya" in draft.text
    assert "flutter developer" in draft.text.lower()
    assert "?" in draft.text
    assert draft.channel == "reply_in_thread"


def test_draft_reads_grammatically_for_engagement_types():
    draft = draft_reply({"hire_target": "software agency"})
    assert "a software agency" in draft.text


def test_draft_flags_agency_requests():
    draft = draft_reply({"hire_target": "software agency"})
    assert any("agency" in n for n in draft.notes or [])


def test_draft_flags_urgency():
    draft = draft_reply({"job_title": "Flutter Developer", "urgency": "High"})
    assert any("urgent" in n.lower() for n in draft.notes or [])


def test_draft_signs_with_your_name():
    assert "Yersultan" in draft_reply({"job_title": "Dev"}, your_name="Yersultan").text


def test_draft_asks_about_budget_when_absent():
    draft = draft_reply({"job_title": "Flutter Developer"})
    assert "budget" in draft.text.lower()


def test_draft_does_not_ask_about_budget_when_stated():
    draft = draft_reply({"job_title": "Flutter Developer", "budget": "$20k", "urgency": "High"})
    assert "budget range" not in draft.text.lower()


# --- End to end -------------------------------------------------------------


def test_triage_finds_leads_and_rejects_the_rest(db, reqs):
    result = triage_text(db, PASTED, reqs, community="flutter-devs")

    assert result.total_posts == 4
    # Dana (needs an app built) and Priya (asking for a referral) are leads.
    # Sam is a job seeker; Jordan posted release notes.
    assert len(result.leads) == 2
    assert result.not_leads == 2

    authors = {lead["author"] for lead in result.leads}
    assert authors == {"Dana Ops", "Priya N"}


def test_triage_extracts_the_details_that_matter(db, reqs):
    leads = triage_text(db, PASTED, reqs, community="flutter-devs").leads
    dana = next(x for x in leads if x["author"] == "Dana Ops")
    assert "Flutter" in dana["skills"]
    assert dana["budget"] is not None
    assert dana["urgency"] == "High"


def test_triage_ranks_by_score(db, reqs):
    leads = triage_text(db, PASTED, reqs, community="flutter-devs").leads
    assert leads == sorted(leads, key=lambda x: x["lead_score"], reverse=True)


def test_triage_attaches_a_reply_draft(db, reqs):
    leads = triage_text(db, PASTED, reqs, community="flutter-devs", your_name="Y").leads
    assert all(lead["reply_draft"] for lead in leads)
    assert all("Y" in lead["reply_draft"] for lead in leads)


def test_re_triaging_the_same_paste_finds_nothing_new(db, reqs):
    """Pasting the same screen twice must not create duplicate leads."""
    first = triage_text(db, PASTED, reqs, community="flutter-devs")
    second = triage_text(db, PASTED, reqs, community="flutter-devs")

    assert len(first.leads) == 2
    assert second.leads == []
    assert second.already_seen == 4


def test_triage_is_searchable_afterwards(db, reqs):
    from circle_leads.export.exporters import query_leads

    triage_text(db, PASTED, reqs, community="flutter-devs")
    with db.session() as s:
        rows = query_leads(s, skills=["Flutter"])
    assert rows
    assert all(r["classification"] == "LEAD" for r in rows)


def test_triage_does_not_mark_a_community_approved(db, reqs):
    """Reading a page yourself is not operator approval for ingestion."""
    from sqlalchemy import select

    from circle_leads.storage.models import Community

    triage_text(db, PASTED, reqs, community="flutter-devs")
    with db.session() as s:
        c = s.scalar(select(Community).where(Community.slug == "flutter-devs"))
        assert c.permission_status == "candidate"
        assert c.is_ingestable is False


def test_triage_handles_empty_input(db, reqs):
    result = triage_text(db, "", reqs)
    assert result.total_posts == 0
    assert result.leads == []
