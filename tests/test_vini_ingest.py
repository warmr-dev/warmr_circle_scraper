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
