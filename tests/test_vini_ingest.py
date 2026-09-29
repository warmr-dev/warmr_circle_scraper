"""Tests for production Vini lead ingest."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import select

from circle_leads.export.vini_ingest import (
    DEFAULT_INGEST_URL,
    PARSER_NAME,
    ViniIngestConfig,
    lead_to_ingest_payload,
    load_vini_ingest_config,
    post_leads_to_vini,
    push_leads_by_ids,
)
from circle_leads.storage.database import Database
from circle_leads.storage.models import Author, Community, Lead, Post


@pytest.fixture
def db(tmp_path):
    return Database(f"sqlite:///{tmp_path}/vini.db")


def _seed_lead(db: Database, *, url: str = "https://acme.circle.so/c/jobs/1") -> int:
    with db.session() as s:
        community = Community(
            slug="acme",
            name="Acme Circle",
            url="https://acme.circle.so",
        )
        s.add(community)
        s.flush()
        author = Author(
            community_id=community.id,
            source_author_id="42",
            display_name="Dana Ops",
        )
        s.add(author)
        s.flush()
        post = Post(
            community_id=community.id,
            author_id=author.id,
            source_content_id="post-1",
            content_type="post",
            content="Looking for an agency to rebuild our app.",
            url=url,
            published_at=datetime(2026, 9, 7, 10, 0, 0),
            dedup_hash="abc",
            classified=True,
        )
        s.add(post)
        s.flush()
        lead = Lead(
            post_id=post.id,
            classification="LEAD",
            confidence=0.9,
            lead_score=80,
            priority="HIGH",
        )
        s.add(lead)
        s.flush()
        return lead.id


def test_load_config_from_env(monkeypatch):
    monkeypatch.setenv("SUPABASE_ANON_KEY", "anon-key")
    monkeypatch.setenv("VINI_API_SECRET", "secret")
    monkeypatch.delenv("VINI_LEADS_INGEST_URL", raising=False)
    cfg = load_vini_ingest_config()
    assert cfg.enabled
    assert cfg.anon_key == "anon-key"
    assert cfg.api_secret == "secret"
    assert cfg.url == DEFAULT_INGEST_URL


def test_payload_shape(db):
    lead_id = _seed_lead(db)
    with db.session() as s:
        lead = s.get(Lead, lead_id)
        post = s.get(Post, lead.post_id)
        community = s.get(Community, post.community_id)
        author = s.get(Author, post.author_id)
        payload = lead_to_ingest_payload(lead, post, community, author)
        fetched_at = post.scraped_at.strftime("%Y-%m-%dT%H:%M:%SZ")
        classified_at = max(lead.created_at.strftime("%Y-%m-%dT%H:%M:%SZ"), fetched_at)

    assert payload == {
        "url": "https://acme.circle.so/c/jobs/1",
        "community": "Acme Circle",
        "content": "Looking for an agency to rebuild our app.",
        "name": "Dana Ops",
        "posted_at": "2026-09-07T10:00:00Z",
        "source_event_at": "2026-09-07T10:00:00Z",
        "fetched_at": fetched_at,
        "classified_at": classified_at,
        "delivery_mode": "live",
        "intent_type": "explicit",
        "platform": "circle",
        "parser": PARSER_NAME,
        "external_id": "circle:acme:lead:post-1",
        "source_author_id": "42",
        "root_message_id": "circle:acme:lead:post-1",
        "source_message_id": "circle:acme:lead:post-1",
    }


def test_a_display_name_stands_in_for_a_missing_member_id(db):
    """The portal rejects a lead with no source_author_id. A Circle member id
    is not always on the row; the name scoped to the community is enough."""
    lead_id = _seed_lead(db)
    with db.session() as s:
        lead = s.get(Lead, lead_id)
        post = s.get(Post, lead.post_id)
        author = s.get(Author, post.author_id)
        author.source_author_id = None
        community = s.get(Community, post.community_id)
        payload = lead_to_ingest_payload(lead, post, community, author)
    assert payload is not None
    assert payload["source_author_id"] == "circle:acme:dana ops"


def test_a_community_homepage_is_not_the_post_url(db):
    """Posts that share the community homepage collapse into one lead."""
    lead_id = _seed_lead(db, url="https://acme.circle.so")
    with db.session() as s:
        lead = s.get(Lead, lead_id)
        post = s.get(Post, lead.post_id)
        community = s.get(Community, post.community_id)
        author = s.get(Author, post.author_id)
        payload = lead_to_ingest_payload(lead, post, community, author)
    assert payload["url"] == "https://acme.circle.so/c/post/post-1"


def test_payload_without_author_name_or_id_is_not_sent(db):
    lead_id = _seed_lead(db)
    with db.session() as s:
        lead = s.get(Lead, lead_id)
        post = s.get(Post, lead.post_id)
        author = s.get(Author, post.author_id)
        author.source_author_id = None
        author.display_name = None
        community = s.get(Community, post.community_id)
        assert lead_to_ingest_payload(lead, post, community, author) is None


def test_payload_requires_url_and_content(db):
    lead_id = _seed_lead(db, url="")
    with db.session() as s:
        lead = s.get(Lead, lead_id)
        post = s.get(Post, lead.post_id)
        post.url = ""
        community = s.get(Community, post.community_id)
        assert lead_to_ingest_payload(lead, post, community, None) is None


def test_post_leads_sends_required_headers():
    cfg = ViniIngestConfig(
        url="https://example.test/ingest",
        anon_key="anon",
        api_secret="sekrit",
    )
    fake = MagicMock()
    fake.post.return_value = MagicMock(status_code=200, text="ok", reason="OK")

    post_leads_to_vini(
        [{"url": "https://x", "content": "y", "parser": PARSER_NAME}],
        config=cfg,
        session=fake,
    )

    fake.post.assert_called_once()
    args, kwargs = fake.post.call_args
    assert args[0] == cfg.url
    assert kwargs["json"][0]["parser"] == PARSER_NAME
    headers = kwargs["headers"]
    assert headers["Content-Type"] == "application/json"
    assert headers["apikey"] == "anon"
    assert headers["Authorization"] == "Bearer anon"
    assert headers["x-vini-api-secret"] == "sekrit"


def test_push_marks_synced(db, monkeypatch):
    lead_id = _seed_lead(db)
    monkeypatch.setenv("SUPABASE_ANON_KEY", "anon")
    monkeypatch.setenv("VINI_API_SECRET", "secret")

    with patch(
        "circle_leads.export.vini_ingest.post_leads_to_vini"
    ) as mock_post:
        with db.session() as s:
            result = push_leads_by_ids(s, [lead_id])
        mock_post.assert_called_once()
        assert result.sent == 1
        assert result.errors == []

    with db.session() as s:
        lead = s.get(Lead, lead_id)
        assert lead.external_synced_at is not None

    # Second push is a no-op without force.
    with patch(
        "circle_leads.export.vini_ingest.post_leads_to_vini"
    ) as mock_post:
        with db.session() as s:
            again = push_leads_by_ids(s, [lead_id])
        mock_post.assert_not_called()
        assert again.sent == 0
        assert again.skipped == 1


def test_push_skipped_without_credentials(db, monkeypatch):
    lead_id = _seed_lead(db)
    monkeypatch.delenv("SUPABASE_ANON_KEY", raising=False)
    monkeypatch.delenv("VINI_SUPABASE_ANON_KEY", raising=False)
    monkeypatch.delenv("VINI_API_SECRET", raising=False)

    with patch(
        "circle_leads.export.vini_ingest.post_leads_to_vini"
    ) as mock_post:
        with db.session() as s:
            result = push_leads_by_ids(s, [lead_id])
        mock_post.assert_not_called()
        assert result.skipped == 1
        assert result.sent == 0


def test_triage_pushes_new_leads(db, monkeypatch):
    from circle_leads.config.settings import Requirements
    from circle_leads.triage.pipeline import triage_records

    monkeypatch.setenv("SUPABASE_ANON_KEY", "anon")
    monkeypatch.setenv("VINI_API_SECRET", "secret")

    # Narrow requirements so the sample hiring post is not filtered.
    reqs = Requirements(
        target_roles=["Flutter Developer", "Mobile Developer"],
        target_skills=["Flutter", "Mobile Development"],
        minimum_confidence=0.5,
        exclude_job_seekers=True,
    )
    records = [
        {
            "content": "We're looking for a Flutter developer to build our mobile app.",
            "url": "https://acme.circle.so/c/jobs/99",
            "author": {"display_name": "Priya"},
            "published_at": datetime(2026, 9, 7, 12, 0, 0, tzinfo=timezone.utc).replace(
                tzinfo=None
            ),
        }
    ]

    with patch(
        "circle_leads.triage.pipeline.push_leads_by_ids"
    ) as mock_push:
        mock_push.return_value = MagicMock(
            sent=1, attempted=1, skipped=0, errors=[]
        )
        result = triage_records(
            db, records, reqs, community="acme",
            source_url="https://acme.circle.so",
        )

    assert len(result.leads) >= 1
    mock_push.assert_called_once()
    pushed_ids = mock_push.call_args.args[1]
    assert len(pushed_ids) >= 1


# --- a 2xx is not delivery -------------------------------------------------
#
# The endpoint answers 200 whether it took the lead or threw it away; the real
# answer is per item, inside the body:
#
#   {"ok": true, "received": 1, "inserted": 0, "accepted": 0, "held": 0,
#    "discarded": 0, "historical_expired": 0,
#    "results": [{"status": "error", "error": "content is required"}]}
#
# The push used to stamp external_synced_at on any 2xx, so a refused lead was
# recorded as delivered and never retried. The client saw nothing for two
# weeks while the dashboard showed leads "sent".

def _reply(body, status=200):
    fake = MagicMock()
    response = MagicMock(status_code=status, reason="OK")
    response.json.return_value = body
    fake.post.return_value = response
    return fake


def _cfg():
    return ViniIngestConfig(url="https://example.test/ingest",
                            anon_key="anon", api_secret="sekrit")


def test_the_per_item_results_are_returned():
    fake = _reply({"ok": True, "received": 1, "inserted": 1,
                   "results": [{"status": "inserted", "id": 7}]})
    out = post_leads_to_vini([{"url": "https://x", "content": "y"}],
                             config=_cfg(), session=fake)
    assert out == [{"status": "inserted", "id": 7}]


def test_a_body_without_results_returns_nothing():
    fake = _reply({"ok": True})
    assert post_leads_to_vini([{"url": "https://x", "content": "y"}],
                              config=_cfg(), session=fake) == []


def test_a_non_json_body_is_not_a_failure():
    fake = MagicMock()
    response = MagicMock(status_code=200, reason="OK")
    response.json.side_effect = ValueError("not json")
    fake.post.return_value = response
    assert post_leads_to_vini([{"url": "https://x", "content": "y"}],
                              config=_cfg(), session=fake) == []


def _push_with(db, monkeypatch, results, lead_ids=None):
    monkeypatch.setenv("SUPABASE_ANON_KEY", "anon")
    monkeypatch.setenv("VINI_API_SECRET", "secret")
    ids = lead_ids or [_seed_lead(db)]
    with patch("circle_leads.export.vini_ingest.post_leads_to_vini") as mock_post:
        mock_post.return_value = results
        with db.session() as s:
            return ids, push_leads_by_ids(s, ids)


def test_a_refused_lead_is_not_marked_delivered(db, monkeypatch):
    ids, result = _push_with(
        db, monkeypatch,
        [{"status": "error", "error": "post_content (or content) is required"}])
    assert result.sent == 0
    assert result.rejected and result.rejected[0][1] == "error"
    with db.session() as s:
        assert s.get(Lead, ids[0]).external_synced_at is None


def test_a_refused_lead_is_tried_again(db, monkeypatch):
    """Leaving external_synced_at NULL is what makes the retry possible."""
    from circle_leads.export.vini_ingest import push_unsynced_leads

    ids, _ = _push_with(db, monkeypatch, [{"status": "discarded"}])
    with patch("circle_leads.export.vini_ingest.post_leads_to_vini") as mock_post:
        mock_post.return_value = [{"status": "inserted"}]
        with db.session() as s:
            again = push_unsynced_leads(s)
    assert again.sent == 1
    with db.session() as s:
        assert s.get(Lead, ids[0]).external_synced_at is not None


@pytest.mark.parametrize("status", ["inserted", "created", "accepted", "held", "skipped"])
def test_the_statuses_that_count_as_delivered(db, monkeypatch, status):
    ids, result = _push_with(db, monkeypatch, [{"status": status}])
    assert result.sent == 1, status
    with db.session() as s:
        assert s.get(Lead, ids[0]).external_synced_at is not None


@pytest.mark.parametrize("status", ["error", "discarded", "historical_expired"])
def test_the_statuses_that_do_not(db, monkeypatch, status):
    ids, result = _push_with(db, monkeypatch, [{"status": status}])
    assert result.sent == 0, status
    with db.session() as s:
        assert s.get(Lead, ids[0]).external_synced_at is None


def _seed_sibling_leads(db, first_lead_id, count):
    """More leads in the community _seed_lead already made (slug is unique)."""
    with db.session() as s:
        post = s.get(Post, s.get(Lead, first_lead_id).post_id)
        community_id, author_id = post.community_id, post.author_id
        made = []
        for n in range(count):
            extra = Post(community_id=community_id, author_id=author_id,
                         source_content_id=f"post-extra-{n}", content_type="post",
                         content="We need a contractor for a rebuild.",
                         url=f"https://acme.circle.so/c/jobs/extra-{n}",
                         published_at=datetime(2026, 9, 8, 10, 0, 0),
                         dedup_hash=f"extra-{n}", classified=True)
            s.add(extra)
            s.flush()
            lead = Lead(post_id=extra.id, classification="LEAD", confidence=0.9,
                        lead_score=80, priority="HIGH")
            s.add(lead)
            s.flush()
            made.append(lead.id)
        return made


def test_a_mixed_batch_splits_correctly(db, monkeypatch):
    first = _seed_lead(db)
    ids = [first] + _seed_sibling_leads(db, first, 2)
    ids, result = _push_with(
        db, monkeypatch,
        [{"status": "inserted"}, {"status": "historical_expired"}, {"status": "inserted"}],
        lead_ids=ids)
    assert result.sent == 2
    assert len(result.rejected) == 1
    assert result.outcomes == {"inserted": 2, "historical_expired": 1}
    with db.session() as s:
        synced = [s.get(Lead, i).external_synced_at is not None for i in ids]
    assert synced == [True, False, True]


def test_an_endpoint_that_says_nothing_per_item_still_marks_delivered(db, monkeypatch):
    """Older endpoint, or a shorter list than we sent: keep the old behaviour."""
    ids, result = _push_with(db, monkeypatch, [])
    assert result.sent == 1
    with db.session() as s:
        assert s.get(Lead, ids[0]).external_synced_at is not None


def test_a_refusal_reaches_a_human(db, monkeypatch):
    sent = []
    monkeypatch.setattr("circle_leads.notify.notify",
                        lambda *a, **k: sent.append((a, k)) or True)
    _push_with(db, monkeypatch, [{"status": "error", "error": "boom"}])
    assert sent, "a lead the client never sees must not be silent"


# --- held is not delivered -------------------------------------------------

def test_a_held_lead_is_reported_not_silently_counted(db, monkeypatch):
    """Vini's own words for a lead it parked:

        {"status": "held", "decision": "invalid_timestamp",
         "holdReason": "missing_source_author_identity"}

    It looked identical to a delivered lead on every dashboard we have, which
    is how two weeks of leads the client never saw went unnoticed.
    """
    sent = []
    monkeypatch.setattr("circle_leads.notify.notify",
                        lambda *a, **k: sent.append((a, k)) or True)
    ids, result = _push_with(db, monkeypatch, [{
        "status": "held",
        "decision": "invalid_timestamp",
        "holdReason": "missing_source_author_identity",
    }])
    assert result.held and result.held[0][0] == ids[0]
    assert "missing_source_author_identity" in result.held[0][1]
    assert sent, "a parked lead must reach a human"


def test_a_stamped_lead_is_sent_again_once_the_author_id_is_included(db, monkeypatch):
    """Leads parked before the payload carried source_author_id stay stamped.

    The first drain after the fix unstamps the ones whose author already has
    an id and posts that id. A second drain does not send them again.
    """
    from circle_leads.export.vini_ingest import drain_unsynced_leads

    lead_id = _seed_lead(db)
    monkeypatch.setenv("SUPABASE_ANON_KEY", "anon")
    monkeypatch.setenv("VINI_API_SECRET", "secret")
    with db.session() as s:
        s.get(Lead, lead_id).external_synced_at = datetime(2026, 9, 21)

    with patch("circle_leads.export.vini_ingest.post_leads_to_vini") as mock_post:
        mock_post.return_value = [{"status": "inserted"}]
        result = drain_unsynced_leads(db)
    assert result.sent == 1
    body = mock_post.call_args.args[0]
    assert body[0]["source_author_id"] == "42"

    with patch("circle_leads.export.vini_ingest.post_leads_to_vini") as mock_post:
        again = drain_unsynced_leads(db)
    mock_post.assert_not_called()
    assert again.sent == 0


def test_a_held_lead_is_not_retried_forever(db, monkeypatch):
    """A retry cannot clear a hold; only fixing the payload can."""
    from circle_leads.export.vini_ingest import push_unsynced_leads

    monkeypatch.setattr("circle_leads.notify.notify", lambda *a, **k: True)
    _push_with(db, monkeypatch, [{"status": "held", "holdReason": "x"}])
    with patch("circle_leads.export.vini_ingest.post_leads_to_vini") as mock_post:
        with db.session() as s:
            push_unsynced_leads(s)
        mock_post.assert_not_called()


# --- fetched_at ------------------------------------------------------------

def test_the_payload_carries_the_time_we_read_the_post(db):
    """The portal parks a lead with no read time:
    {"status": "held", "decision": "invalid_timestamp",
     "holdReason": "missing_or_invalid_fetched_at"}."""
    lead_id = _seed_lead(db)
    with db.session() as s:
        lead = s.get(Lead, lead_id)
        post = s.get(Post, lead.post_id)
        post.scraped_at = datetime(2026, 9, 28, 8, 4, 49)
        payload = lead_to_ingest_payload(
            lead, post, s.get(Community, post.community_id), s.get(Author, post.author_id))
    assert payload["fetched_at"] == "2026-09-28T08:04:49Z"


def test_a_fetched_at_hold_on_a_body_without_it_is_sent_again(db, monkeypatch):
    """A body that lacked fetched_at can still be fixed, so it stays unsynced."""
    monkeypatch.setattr("circle_leads.notify.notify", lambda *a, **k: True)
    hold = {"status": "held", "decision": "invalid_timestamp",
            "holdReason": "missing_or_invalid_fetched_at"}
    with patch("circle_leads.export.vini_ingest.lead_to_ingest_payload",
               return_value={"url": "u", "source_author_id": "42"}):
        ids, result = _push_with(db, monkeypatch, [hold])
    assert result.held and result.sent == 0
    with db.session() as s:
        assert s.get(Lead, ids[0]).external_synced_at is None


def test_a_fetched_at_hold_after_sending_it_is_not_retried(db, monkeypatch):
    monkeypatch.setattr("circle_leads.notify.notify", lambda *a, **k: True)
    ids, result = _push_with(db, monkeypatch, [{
        "status": "held", "decision": "invalid_timestamp",
        "holdReason": "missing_or_invalid_fetched_at"}])
    assert result.sent == 1
    with db.session() as s:
        assert s.get(Lead, ids[0]).external_synced_at is not None


# --- classified_at ---------------------------------------------------------

def test_the_payload_carries_the_time_we_judged_the_post(db):
    """{"status": "held", "decision": "invalid_timestamp",
        "holdReason": "missing_or_invalid_classified_at"}, 2026-09-28."""
    lead_id = _seed_lead(db)
    with db.session() as s:
        lead = s.get(Lead, lead_id)
        post = s.get(Post, lead.post_id)
        post.scraped_at = datetime(2026, 9, 28, 8, 4, 49)
        lead.created_at = datetime(2026, 9, 28, 8, 5, 30)
        payload = lead_to_ingest_payload(
            lead, post, s.get(Community, post.community_id), s.get(Author, post.author_id))
    assert payload["classified_at"] == "2026-09-28T08:05:30Z"


def test_classified_at_never_falls_before_fetched_at(db):
    """A post read again after its verdict has a later scraped_at."""
    lead_id = _seed_lead(db)
    with db.session() as s:
        lead = s.get(Lead, lead_id)
        post = s.get(Post, lead.post_id)
        lead.created_at = datetime(2026, 9, 28, 8, 0, 0)
        post.scraped_at = datetime(2026, 9, 28, 11, 0, 0)
        payload = lead_to_ingest_payload(
            lead, post, s.get(Community, post.community_id), s.get(Author, post.author_id))
    assert payload["classified_at"] == payload["fetched_at"] == "2026-09-28T11:00:00Z"


def test_a_classified_at_hold_on_a_body_without_it_is_sent_again(db, monkeypatch):
    monkeypatch.setattr("circle_leads.notify.notify", lambda *a, **k: True)
    hold = {"status": "held", "decision": "invalid_timestamp",
            "holdReason": "missing_or_invalid_classified_at"}
    with patch("circle_leads.export.vini_ingest.lead_to_ingest_payload",
               return_value={"url": "u", "source_author_id": "42", "fetched_at": "x"}):
        ids, result = _push_with(db, monkeypatch, [hold])
    assert result.held and result.sent == 0
    with db.session() as s:
        assert s.get(Lead, ids[0]).external_synced_at is None


def test_push_leads_resends_exactly_the_given_ids(tmp_path, monkeypatch):
    """``push-leads --id`` resends leads the portal parked before a fix,
    even though they are stamped synced, and touches no other lead."""
    from click.testing import CliRunner

    from circle_leads.cli.main import cli

    url = f"sqlite:///{tmp_path}/cli.db"
    db = Database(url)
    first = _seed_lead(db)
    second, third = _seed_sibling_leads(db, first, 2)
    with db.session() as s:
        for i in (first, second, third):
            s.get(Lead, i).external_synced_at = datetime(2026, 9, 28)
    monkeypatch.setenv("SUPABASE_ANON_KEY", "anon")
    monkeypatch.setenv("VINI_API_SECRET", "secret")
    with patch("circle_leads.export.vini_ingest.post_leads_to_vini") as mock_post:
        mock_post.return_value = [{"status": "inserted"}, {"status": "inserted"}]
        out = CliRunner().invoke(
            cli, ["--db", url, "push-leads", "--id", str(first), "--id", str(third)])
    assert out.exit_code == 0, out.output
    sent = mock_post.call_args.args[0]
    assert [p["external_id"] for p in sent] == [
        "circle:acme:lead:post-1", "circle:acme:lead:post-extra-1"]


# --- Vini's answer is kept on the lead ---------------------------------------
#
# external_synced_at is set both for a lead Vini published and for one it
# parked, so "which leads does the client actually see" had no answer outside
# the worker's log. The answer now lives on the lead (vini_status/reason/ref).

def _answer(db, lead_id):
    with db.session() as s:
        lead = s.get(Lead, lead_id)
        return (lead.vini_status, lead.vini_reason, lead.vini_ref,
                lead.vini_attempts, lead.vini_responded_at is not None)


@pytest.mark.parametrize("status, expected", [
    ("inserted", "accepted"), ("created", "accepted"), ("accepted", "accepted"),
    ("ok", "accepted"), ("success", "accepted"),
    ("duplicate", "duplicate"), ("skipped", "duplicate"),
])
def test_a_landed_answer_is_kept(db, monkeypatch, status, expected):
    ids, _ = _push_with(db, monkeypatch, [{"status": status, "id": 7}])
    assert _answer(db, ids[0]) == (expected, status, "7", 1, True)


def test_a_hold_is_kept_with_vinis_reason(db, monkeypatch):
    ids, _ = _push_with(db, monkeypatch, [{
        "status": "held", "decision": "invalid_timestamp",
        "holdReason": "missing_source_author_identity"}])
    status, reason, _ref, attempts, answered = _answer(db, ids[0])
    assert (status, attempts, answered) == ("held", 1, True)
    assert reason == "invalid_timestamp missing_source_author_identity"


@pytest.mark.parametrize("item, reason", [
    ({"status": "error", "error": "content is required"}, "error: content is required"),
    ({"status": "historical_expired"}, "historical_expired"),
    ({"status": "discarded", "reason": "spam"}, "discarded: spam"),
])
def test_a_refusal_is_kept_with_its_reason(db, monkeypatch, item, reason):
    ids, _ = _push_with(db, monkeypatch, [item])
    assert _answer(db, ids[0])[:2] == ("rejected", reason)


def test_no_answer_per_item_counts_as_accepted_and_says_so(db, monkeypatch):
    ids, _ = _push_with(db, monkeypatch, [])
    assert _answer(db, ids[0])[:2] == ("accepted", "no_item_result")


def test_a_transport_error_is_an_error_until_vini_answers(db, monkeypatch):
    monkeypatch.setenv("SUPABASE_ANON_KEY", "anon")
    monkeypatch.setenv("VINI_API_SECRET", "secret")
    lead_id = _seed_lead(db)
    with patch("circle_leads.export.vini_ingest.post_leads_to_vini",
               side_effect=RuntimeError("Vini ingest HTTP 503: down")):
        with db.session() as s:
            push_leads_by_ids(s, [lead_id])
    status, reason, _ref, attempts, answered = _answer(db, lead_id)
    assert (status, attempts, answered) == ("error", 1, False)
    assert "HTTP 503" in reason


def test_a_transport_error_does_not_erase_an_earlier_answer(db, monkeypatch):
    """A timeout says nothing about what Vini thinks of the lead."""
    monkeypatch.setenv("SUPABASE_ANON_KEY", "anon")
    monkeypatch.setenv("VINI_API_SECRET", "secret")
    ids, _ = _push_with(db, monkeypatch, [{
        "status": "held", "decision": "needs_review", "holdReason": "manual"}])
    with patch("circle_leads.export.vini_ingest.post_leads_to_vini",
               side_effect=RuntimeError("timeout")):
        with db.session() as s:
            push_leads_by_ids(s, ids, force=True)
    status, reason, _ref, attempts, _answered = _answer(db, ids[0])
    assert (status, reason, attempts) == ("held", "needs_review manual", 2)


def _export_rows(db):
    from circle_leads.storage.models import ActivityLog

    with db.session() as s:
        return [(a.level, a.summary, a.detail) for a in s.scalars(
            select(ActivityLog).where(ActivityLog.kind == "export").order_by(ActivityLog.id))]


def test_a_changed_answer_is_logged_once(db, monkeypatch):
    """The watcher drains after every batch and a refused lead goes out again
    each time: a row per push would bury the log in identical copies."""
    from circle_leads.export.vini_ingest import push_unsynced_leads

    ids, _ = _push_with(db, monkeypatch, [{"status": "discarded", "reason": "spam"}])
    with patch("circle_leads.export.vini_ingest.post_leads_to_vini") as mock_post:
        mock_post.return_value = [{"status": "discarded", "reason": "spam"}]
        with db.session() as s:
            push_unsynced_leads(s)
    rows = _export_rows(db)
    assert len(rows) == 1
    level, summary, detail = rows[0]
    assert level == "warning"
    assert summary == "Vini answered: rejected 1"
    assert detail["rejected_ids"] == str(ids[0])
    assert detail["reasons"] == {"discarded: spam": 1}

    # A different answer is news again.
    with patch("circle_leads.export.vini_ingest.post_leads_to_vini") as mock_post:
        mock_post.return_value = [{"status": "inserted"}]
        with db.session() as s:
            push_unsynced_leads(s)
    assert [r[0] for r in _export_rows(db)] == ["warning", "success"]


def test_a_mixed_batch_is_one_row_with_ids_per_answer(db, monkeypatch):
    first = _seed_lead(db)
    ids = [first] + _seed_sibling_leads(db, first, 2)
    _push_with(db, monkeypatch, [
        {"status": "inserted"},
        {"status": "held", "decision": "invalid_timestamp", "holdReason": "x"},
        {"status": "error", "error": "boom"},
    ], lead_ids=ids)
    rows = _export_rows(db)
    assert len(rows) == 1
    level, _summary, detail = rows[0]
    assert level == "warning"
    assert detail["counts"] == {"accepted": 1, "held": 1, "rejected": 1}
    assert detail["accepted_ids"] == str(ids[0])
    assert detail["held_ids"] == str(ids[1])
    assert detail["rejected_ids"] == str(ids[2])


def test_the_triage_path_leaves_one_export_row(db, monkeypatch):
    """The push logs for every caller now; the triage path must not add its
    own copy of the same row."""
    from circle_leads.config.settings import Requirements
    from circle_leads.triage.pipeline import triage_records

    monkeypatch.setenv("SUPABASE_ANON_KEY", "anon")
    monkeypatch.setenv("VINI_API_SECRET", "secret")
    reqs = Requirements(
        target_roles=["Flutter Developer", "Mobile Developer"],
        target_skills=["Flutter", "Mobile Development"],
        minimum_confidence=0.5,
        exclude_job_seekers=True,
    )
    records = [{
        "content": "We're looking for a Flutter developer to build our mobile app.",
        "url": "https://acme.circle.so/c/jobs/99",
        "author": {"display_name": "Priya"},
        "published_at": datetime.now(timezone.utc).replace(tzinfo=None),
    }]
    with patch("circle_leads.export.vini_ingest.post_leads_to_vini") as mock_post:
        mock_post.return_value = [{"status": "inserted", "id": 11}]
        result = triage_records(db, records, reqs, community="acme",
                                source_url="https://acme.circle.so")
    assert result.leads
    rows = _export_rows(db)
    assert len(rows) == 1 and rows[0][1] == "Vini answered: accepted 1"
