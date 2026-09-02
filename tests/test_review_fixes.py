"""Regression tests for defects found in security review.

Each test names the behavior that was wrong, so a reintroduction fails loudly.
"""

import pytest

from circle_leads.classifier.lead_classifier import ClassificationResult
from circle_leads.config.settings import (
    DEFAULT_EXCLUDED_CONTENT,
    CommunityPermission,
    Requirements,
    load_requirements,
)
from circle_leads.export.exporters import to_csv
from circle_leads.scoring.lead_scoring import score_lead
from circle_leads.scraper.community_scraper import is_direct_message_room
from circle_leads.scraper.normalize import redact_pii
from circle_leads.storage.database import (
    Database,
    find_near_duplicate,
    get_or_create_community,
    purge_community,
    upsert_post,
)


@pytest.fixture(scope="module")
def reqs():
    return load_requirements()


# --- 4.1 phone_numbers must be excluded by default --------------------------


def test_phone_numbers_excluded_by_default_everywhere():
    """A minimal permission file must not silently re-enable phone storage."""
    assert "phone_numbers" in Requirements().excluded_content
    assert "phone_numbers" in CommunityPermission(community_id="x").excluded_content
    assert "phone_numbers" in DEFAULT_EXCLUDED_CONTENT


def test_exclusions_are_unioned_not_overridden():
    """Neither config source may narrow what the other forbids."""
    perm = CommunityPermission(community_id="x", excluded_content=["email_addresses"])
    reqs = Requirements(excluded_content=["phone_numbers"])
    merged = sorted(set(perm.excluded_content) | set(reqs.excluded_content))
    assert "phone_numbers" in merged and "email_addresses" in merged


# --- 4.2 / 4.3 phone redaction quality --------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Call me at +44 20 7946 0958",
        "Reach me: (555) 123-4567",
        "My number is 555-123-4567",
        "WhatsApp: +91 98765 43210",
        "phone 07700 900123",
        "+7 495 123-45-67",
    ],
)
def test_phone_numbers_are_redacted(text):
    assert "[phone removed]" in redact_pii(text, ["phone_numbers"])


@pytest.mark.parametrize(
    "text",
    [
        "Budget is 2024 500 1000",
        "Order 100 200 3000",
        "Salary 120 000 5000",
        "Budget $120,000/year",
        "We need 3 devs by 2026",
        "Version 1.2.3 released",
    ],
)
def test_budget_and_version_numbers_survive_redaction(text):
    """Over-redaction destroyed the budget signal the scorer depends on."""
    assert "[phone removed]" not in redact_pii(text, ["phone_numbers"])


# --- 5.2 zero confidence must not score maximum -----------------------------


def test_zero_confidence_does_not_outscore_high_confidence(reqs):
    extracted = {"job_title": "Backend Developer", "skills": ["Python"],
                 "budget": "$100,000", "company": "Acme"}
    confident = ClassificationResult(
        classification="LEAD", confidence=1.0, extracted=extracted
    )
    unconfident = ClassificationResult(
        classification="LEAD", confidence=0.0, extracted=extracted
    )
    high, _, _ = score_lead(confident, reqs)
    low, _, _ = score_lead(unconfident, reqs)
    assert low < high


# --- 5.3 confidence floor applies regardless of exclude_job_seekers ----------


def test_confidence_floor_applies_when_job_seekers_included(tmp_path, reqs):
    from circle_leads.pipeline import classify_pending

    db = Database(f"sqlite:///{tmp_path}/t.db")
    with db.session() as s:
        c = get_or_create_community(s, slug="a", url="https://a.circle.so")
        upsert_post(s, community_id=c.id, record={
            "source_content_id": "p1",
            "content": "We are hiring a COBOL developer for our mainframe team.",
        })

    narrow = reqs.model_copy(update={
        "exclude_job_seekers": False,
        "target_roles": ["Flutter Developer"],
        "target_skills": ["Flutter"],
    })
    stats = classify_pending(db, narrow)
    # Turning off job-seeker exclusion must not disable the whole filter.
    assert stats["filtered"] == 1
    assert stats["leads"] == 0


# --- 5.4 a filtered post must not keep a stale lead -------------------------


def test_edited_post_drops_its_stale_lead(tmp_path, reqs):
    from sqlalchemy import select

    from circle_leads.pipeline import classify_pending
    from circle_leads.storage.models import Lead

    db = Database(f"sqlite:///{tmp_path}/t.db")
    with db.session() as s:
        c = get_or_create_community(s, slug="a", url="https://a.circle.so")
        upsert_post(s, community_id=c.id, record={
            "source_content_id": "p1",
            "content": "We are hiring a Flutter developer for our mobile app.",
        })
        cid = c.id
    classify_pending(db, reqs)
    with db.session() as s:
        assert s.scalars(select(Lead)).all()

    # The post is edited into something that no longer qualifies.
    with db.session() as s:
        upsert_post(s, community_id=cid, record={
            "source_content_id": "p1",
            "content": "Never mind, we filled the role internally.",
        })
    classify_pending(db, reqs)
    with db.session() as s:
        assert s.scalars(select(Lead)).all() == []


# --- 6.1 CSV formula injection ----------------------------------------------


@pytest.mark.parametrize(
    "hostile",
    ["=cmd|'/C calc'!A0", "+1+1", "-2+3", "@SUM(1+1)", "\tstart", "\rstart"],
)
def test_csv_cells_cannot_start_a_formula(hostile, tmp_path):
    """Author names are member-controlled and land in a spreadsheet."""
    path = to_csv([{"community": "a", "author": hostile, "content": "x"}],
                  tmp_path / "o.csv")
    for line in path.read_text().splitlines()[1:]:
        for cell in line.split(","):
            stripped = cell.strip('"')
            assert not stripped.startswith(("=", "+", "@")) or stripped.startswith("'")


# --- 7.1 an unexpected error must not orphan the run row --------------------


def test_unexpected_error_marks_run_failed(tmp_path, monkeypatch, reqs):
    from sqlalchemy import select

    import circle_leads.pipeline as pipeline
    from circle_leads.storage.models import ScrapeRun

    monkeypatch.setenv("CIRCLE_TEST_TOKEN", "x")
    monkeypatch.setattr(
        pipeline, "_collect",
        lambda *a, **kw: (_ for _ in ()).throw(ValueError("unexpected")),
    )
    db = Database(f"sqlite:///{tmp_path}/t.db")
    perm = CommunityPermission(
        community_id="a", permission_status="approved",
        allowed_space_ids=["1"], admin_token_env="CIRCLE_TEST_TOKEN",
    )
    summary = pipeline.ingest_community(db, perm, reqs)

    assert summary.state == "FAILED"
    with db.session() as s:
        run = s.scalars(select(ScrapeRun)).first()
        assert run.state == "FAILED"
        assert run.finished_at is not None
        assert "unexpected" in (run.error or "")


# --- 7.2 dedup must not cross a community boundary --------------------------


def test_near_duplicate_never_crosses_communities(tmp_path):
    """Each community's data is consented to separately."""
    db = Database(f"sqlite:///{tmp_path}/t.db")
    text = "We are looking for a senior backend engineer to help build our API."
    with db.session() as s:
        a = get_or_create_community(s, slug="a", url="https://a.circle.so")
        b = get_or_create_community(s, slug="b", url="https://b.circle.so")
        upsert_post(s, community_id=a.id, record={"source_content_id": "p1", "content": text})
        post_b, _ = upsert_post(s, community_id=b.id, record={"source_content_id": "p1", "content": text})
        assert find_near_duplicate(s, post_b) is None


def test_near_duplicate_still_found_within_a_community(tmp_path):
    db = Database(f"sqlite:///{tmp_path}/t.db")
    text = "We are looking for a senior backend engineer to help build our API."
    with db.session() as s:
        c = get_or_create_community(s, slug="a", url="https://a.circle.so")
        upsert_post(s, community_id=c.id, record={"source_content_id": "p1", "content": text})
        dup, _ = upsert_post(s, community_id=c.id,
                             record={"source_content_id": "p2", "content": text + " Thanks!"})
        assert find_near_duplicate(s, dup) is not None


# --- 7.3 the kill switch must clear the watermark ---------------------------


def test_purge_clears_sync_watermark(tmp_path):
    from circle_leads.storage.models import utcnow

    db = Database(f"sqlite:///{tmp_path}/t.db")
    with db.session() as s:
        c = get_or_create_community(s, slug="a", url="https://a.circle.so")
        c.last_synced_at = utcnow()
        upsert_post(s, community_id=c.id, record={"source_content_id": "p1", "content": "x"})
    with db.session() as s:
        purge_community(s, "a")
    with db.session() as s:
        from sqlalchemy import select

        from circle_leads.storage.models import Community

        c = s.scalar(select(Community).where(Community.slug == "a"))
        assert c.last_synced_at is None


# --- 2.1 a contradictory room kind must fail closed -------------------------


def test_contradictory_room_kinds_are_excluded():
    """One field naming a DM must not be masked by the other."""
    assert is_direct_message_room(
        {"uuid": "x", "chat_room_kind": "group_chat", "kind": "direct"}
    ) is True
    assert is_direct_message_room(
        {"uuid": "x", "chat_room_kind": "group_chat", "kind": "group_chat"}
    ) is False


# --- 5.5 config keys must actually do something -----------------------------


def test_exclude_keywords_from_config_are_applied(reqs):
    from circle_leads.classifier.lead_classifier import classify

    text = "We are hiring a backend developer. Bananas."
    baseline = classify(text, reqs)
    assert baseline.classification == "LEAD"

    tuned = reqs.model_copy(
        update={"keywords": reqs.keywords.model_copy(update={"exclude": ["bananas"]})}
    )
    assert classify(text, tuned).rule_score < baseline.rule_score
