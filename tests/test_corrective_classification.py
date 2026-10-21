"""Bounded repairs never turn invalid evidence into a durable business decision."""
import json

import pytest
from sqlalchemy import select

from circle_leads.classifier.ai_classifier import classify_with_llm
from circle_leads.classifier.decisions import evaluate_post
from circle_leads.config.settings import load_requirements
from circle_leads.storage.database import Database, get_or_create_community, upsert_post
from circle_leads.storage.models import ActivityLog, Lead, Post
from tests.test_commercial_demand import answer


class SequenceBackend:
    model = "fixture-model"

    def __init__(self, *responses):
        self.responses = iter(responses)
        self.calls = []

    def complete(self, system, user):
        self.calls.append((system, json.loads(user)))
        response = next(self.responses)  # Any unexpected third call fails the test.
        if isinstance(response, Exception):
            raise response
        return response if isinstance(response, str) else json.dumps(response)


TEXT = "Could someone recommend a software agency for our project?"
BAD = {"supporting_excerpts": [{"source_id": "current", "quote": "invented quote"}]}


@pytest.mark.parametrize("bad", [
    BAD,
    {"need_owner": "none"},
    {"demand_signal": "none"},
    {"supporting_excerpts": [{"source_id": "post:9", "quote": "We need an agency"}]},
    {"supporting_excerpts": [{"source_id": "invented", "quote": TEXT}]},
    {"supporting_excerpts": [{"source_id": [], "quote": TEXT}]},
    {"confidence": "not a number"},
])
def test_repair_revalidates_complete_response_and_preserves_inputs(bad):
    context = [{"source_id": "post:9", "content": "We need an agency"}]
    backend = SequenceBackend(answer(TEXT, **bad), answer(TEXT))
    verdict = classify_with_llm(TEXT, backend, context=context, current_metadata={"author_id": 12})
    assert verdict.classification == "LEAD" and not verdict.error
    assert len(backend.calls) == 2
    assert backend.calls[0][0] == backend.calls[1][0]
    original, correction = backend.calls[0][1], backend.calls[1][1]
    assert correction["current_post"] == original["current_post"]
    assert correction["context"] == original["context"]
    assert correction["correction"]["validation_error"]
    assert [x["kind"] for x in verdict.diagnostics] == ["validation", "valid"]
    assert verdict.supporting_excerpts == [{"source_id": "current", "quote": TEXT}]


@pytest.mark.parametrize("raw", ["not JSON", "[]", '{"confidence":'])
def test_malformed_json_has_one_corrective_attempt(raw):
    backend = SequenceBackend(raw, answer(TEXT))
    verdict = classify_with_llm(TEXT, backend)
    assert not verdict.error and len(backend.calls) == 2
    assert verdict.diagnostics[0]["response_sha256"]


def test_success_and_provider_failures_do_not_trigger_correction():
    good = SequenceBackend(answer(TEXT))
    assert not classify_with_llm(TEXT, good).error
    assert len(good.calls) == 1
    down = SequenceBackend(TimeoutError("transport unavailable"))
    v = classify_with_llm(TEXT, down)
    assert v.failure_kind == "provider" and len(down.calls) == 1
    assert v.diagnostics == [{"attempt": 1, "kind": "provider", "exception": "TimeoutError"}]


def test_provider_outage_during_repair_is_not_misclassified_as_validation_exhaustion():
    backend = SequenceBackend(answer(TEXT, **BAD), TimeoutError("transport unavailable"))
    v = classify_with_llm(TEXT, backend)
    assert v.failure_kind == "provider" and v.classification == "UNCERTAIN"
    assert [x["kind"] for x in v.diagnostics] == ["validation", "provider"]


@pytest.mark.parametrize("text", ["Ramp!", "@Example Person", "Congratulations on the new arrival!"])
def test_reply_can_be_corrected_to_negative_without_transferring_root_demand(text):
    root = [{"source_id": "post:9", "content": TEXT}]
    bad = answer(text, supporting_excerpts=[{"source_id": "post:9", "quote": TEXT}])
    backend = SequenceBackend(bad, answer(text, positive=False))
    v = classify_with_llm(text, backend, context=root)
    assert v.classification == "NOT_LEAD" and not v.error
    assert v.evidence_quote == text


def test_fabricated_context_evidence_remains_invalid_on_corrective_attempt():
    bad = answer(TEXT, supporting_excerpts=[{"source_id": "post:9", "quote": "invented"}])
    backend = SequenceBackend(bad, bad)
    v = classify_with_llm(TEXT, backend, context=[{"source_id": "post:9", "content": TEXT}])
    assert v.failure_kind == "validation" and v.error == "unverified_evidence"
    assert v.classification == "UNCERTAIN" and v.supporting_excerpts == []
    assert len(v.diagnostics) == len(backend.calls) == 2


def test_diagnostics_are_bounded_and_not_approved_evidence():
    bad = answer(TEXT, supporting_excerpts=[{"source_id": "x" * 500, "quote": "y" * 2000}] * 30)
    backend = SequenceBackend(bad, bad)
    v = classify_with_llm(TEXT, backend)
    for d in v.diagnostics:
        assert len(d["rejected_excerpts"]) == 5
        assert len(d["rejected_excerpts"][0]["quote"]) == 500
        assert len(d["rejected_excerpts"][0]["source_id"]) == 128
        assert d["rejected_excerpts"][0]["reason"] == "unknown_source_id"
        assert "raw" not in d
    assert not v.supporting_excerpts and v.evidence_quote is None


@pytest.fixture
def stored(tmp_path):
    db = Database(f"sqlite:///{tmp_path}/db")
    with db.session() as s:
        c = get_or_create_community(s, slug="fixture", url="https://fixture.circle.so")
        p, _ = upsert_post(s, community_id=c.id, record={"source_content_id": "p", "content": TEXT})
        pid, cid = p.id, c.id
    return db, pid, cid


def test_exhaustion_is_durable_visible_and_cannot_publish_or_starve_batch(stored, monkeypatch):
    import circle_leads.pipeline as batch
    db, pid, cid = stored
    backend = SequenceBackend(answer(TEXT, **BAD), answer(TEXT, **BAD))
    monkeypatch.setattr(batch, "make_backend", lambda: backend)
    monkeypatch.setattr(batch, "push_leads_by_ids", lambda *a, **k: pytest.fail("unexpected publication"))
    batch.classify_pending(db, load_requirements(), use_llm=True, export=True)
    with db.session() as s:
        p = s.get(Post, pid)
        audit = p.classification_audit
        assert not p.classified and p.classification_retry_at is None
        assert audit["outcome"] == "error" and audit["retry_state"] == "needs_review"
        assert audit["failure_kind"] == "validation" and audit["excerpts"] == []
        assert audit["diagnostics"][0]["rejected_excerpts"][0]["quote"] == "invented quote"
        activity = s.scalar(select(ActivityLog).where(ActivityLog.kind == "classify"))
        assert activity.detail["diagnostics"] == audit["diagnostics"]
        # A recapture cannot spend a new two-call budget on identical input.
        result = evaluate_post(s, p, load_requirements(), llm=backend)
        assert result.llm_error and p.classification_audit == audit
        assert not s.scalar(select(Lead))
        upsert_post(s, community_id=cid, record={"source_content_id": "next", "content": TEXT})
    backend.responses = iter([answer(TEXT, positive=False)])
    stats = batch.classify_pending(db, load_requirements(), use_llm=True, limit=1, export=False)
    assert stats["classified"] == 1  # Held first row does not consume the batch limit.
    assert len(backend.calls) == 3
    backend.responses = iter(())
    batch.classify_pending(db, load_requirements(), use_llm=True, retry_only=True, export=False)
    assert len(backend.calls) == 3


def test_changed_source_releases_review_hold_and_preserves_diagnostic_history(stored, monkeypatch):
    import circle_leads.pipeline as batch
    db, pid, cid = stored
    backend = SequenceBackend(answer(TEXT, **BAD), answer(TEXT, **BAD))
    monkeypatch.setattr(batch, "make_backend", lambda: backend)
    batch.classify_pending(db, load_requirements(), use_llm=True, export=False)
    updated = "Could someone recommend a design agency for our project?"
    with db.session() as s:
        upsert_post(s, community_id=cid, record={"source_content_id": "p", "content": updated})
    backend.responses = iter([answer(updated)])
    batch.classify_pending(db, load_requirements(), use_llm=True, export=False)
    with db.session() as s:
        p = s.get(Post, pid)
        assert p.classified and p.classification_audit["retry_state"] == "complete"
        assert p.classification_audit["attempt"] == 1
        events = s.scalars(select(ActivityLog).where(ActivityLog.kind == "classify").order_by(ActivityLog.id)).all()
        assert events[0].detail["retry_state"] == "needs_review"
        assert events[0].detail["diagnostics"][0]["rejected_excerpts"]
        assert len(events) == 2


def test_provider_errors_keep_durable_backoff_instead_of_review_hold(stored):
    db, pid, _ = stored
    backend = SequenceBackend(TimeoutError("unavailable"))
    with db.session() as s:
        p = s.get(Post, pid)
        evaluate_post(s, p, load_requirements(), llm=backend)
        assert not p.classified and p.classification_retry_at is not None
        assert p.classification_audit["retry_state"] == "scheduled"
        assert p.classification_audit["failure_kind"] == "provider"


def test_corrective_payload_respects_context_limit(monkeypatch):
    import circle_leads.classifier.ai_classifier as ai
    original = json.dumps({"current_post": {"source_id": "current", "content": TEXT}, "context": [],
                           "evidence_spans": list(ai._evidence_spans({"current": TEXT}).values())})
    monkeypatch.setattr(ai, "MAX_CONTEXT_CHARS", len(original) + 10)
    backend = SequenceBackend(answer(TEXT, **BAD))
    v = classify_with_llm(TEXT, backend)
    assert v.failure_kind == "validation" and len(backend.calls) == 1
    assert v.diagnostics[0]["repair_skipped"] == "context_limit_exceeded"


def test_repaired_success_persists_both_attempts_and_only_verified_evidence(stored):
    db, pid, _ = stored
    backend = SequenceBackend(answer(TEXT, **BAD), answer(TEXT))
    with db.session() as s:
        p = s.get(Post, pid)
        evaluate_post(s, p, load_requirements(), llm=backend)
        assert p.classified and p.classification_audit["retry_state"] == "complete"
        audit = p.classification_audit
        assert [d["kind"] for d in audit["diagnostics"]] == ["validation", "valid"]
        assert audit["excerpts"][0]["quote"] == TEXT
        assert audit["diagnostics"][0]["rejected_excerpts"][0]["quote"] == "invented quote"
        assert p.classification_retry_at is None


def test_activity_diagnostics_preserve_structure_with_size_and_secret_key_limits():
    from circle_leads.storage.activity import _safe_detail
    entry = {"kind": "validation", "api_key": "do-not-store",
             "rejected_excerpts": [{"quote": "x" * 2000, "token": "do-not-store"}] * 20}
    safe = _safe_detail({"diagnostics": [entry] * 10, "password": "do-not-store"})
    assert "password" not in safe
    assert len(safe["diagnostics"]) == 2
    d = safe["diagnostics"][0]
    assert "api_key" not in d
    assert len(d["rejected_excerpts"]) == 10
    assert len(d["rejected_excerpts"][0]["quote"]) == 500
    assert "token" not in d["rejected_excerpts"][0]


def test_missing_current_evidence_diagnostic_keeps_context_quote_and_why_it_was_rejected():
    context = [{"source_id": "post:9", "content": TEXT}]
    bad = answer("Thanks!", supporting_excerpts=[{"source_id": "post:9", "quote": TEXT}])
    v = classify_with_llm("Thanks!", SequenceBackend(bad, bad), context=context)
    assert v.error == "Evidence must include the current post."
    assert v.diagnostics[0]["rejected_excerpts"] == [
        {"source_id": "post:9", "quote": TEXT, "reason": "current_post_evidence_missing"}]
    assert not v.supporting_excerpts
