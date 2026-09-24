"""Tests for production Vini lead ingest."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

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

    assert payload == {
        "url": "https://acme.circle.so/c/jobs/1",
        "community": "Acme Circle",
        "content": "Looking for an agency to rebuild our app.",
        "name": "Dana Ops",
        "posted_at": "2026-09-07T10:00:00Z",
        "intent_type": "explicit",
        "platform": "circle",
        "parser": PARSER_NAME,
        "external_id": "circle:acme:post:post-1",
    }


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


@pytest.mark.parametrize("status", ["inserted", "accepted", "held", "skipped"])
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


def test_a_held_lead_is_not_retried_forever(db, monkeypatch):
    """A retry cannot clear a hold; only fixing the payload can."""
    from circle_leads.export.vini_ingest import push_unsynced_leads

    monkeypatch.setattr("circle_leads.notify.notify", lambda *a, **k: True)
    _push_with(db, monkeypatch, [{"status": "held", "holdReason": "x"}])
    with patch("circle_leads.export.vini_ingest.post_leads_to_vini") as mock_post:
        with db.session() as s:
            push_unsynced_leads(s)
        mock_post.assert_not_called()
