"""Guardrails on the LLM layer: a model must ground its claims in the source."""

import json

import pytest

from circle_leads.classifier.ai_classifier import (
    classify_with_llm,
    verify_evidence,
)
from circle_leads.classifier.lead_classifier import classify
from circle_leads.config.settings import load_requirements


class StubBackend:
    """Returns a canned model response."""

    def __init__(self, payload, raise_exc=None):
        self.payload = payload
        self.raise_exc = raise_exc
        self.calls = 0

    def complete(self, system, user):
        self.calls += 1
        if self.raise_exc:
            raise self.raise_exc
        return json.dumps(self.payload) if isinstance(self.payload, dict) else self.payload


POST = "We are a startup and are looking for a senior backend engineer to build the API."


def test_verify_evidence_accepts_verbatim_span():
    assert verify_evidence("looking for a senior backend engineer", POST)


def test_verify_evidence_tolerates_whitespace_differences():
    assert verify_evidence("looking  for a senior\nbackend engineer", POST)


def test_verify_evidence_rejects_invented_quote():
    assert not verify_evidence("we will pay $200,000 for this role", POST)
    assert not verify_evidence(None, POST)


def test_lead_with_fabricated_evidence_is_downgraded():
    """A LEAD the model cannot ground in real words must not be trusted."""
    backend = StubBackend({
        "classification": "LEAD",
        "confidence": 0.99,
        "reason": "They are hiring.",
        "evidence_quote": "we have a budget of $500,000",  # not in the post
    })
    verdict = classify_with_llm(POST, backend)
    assert verdict.classification == "UNCERTAIN"
    assert "unverified_evidence" in verdict.disqualifiers


def test_unsupported_budget_and_company_are_dropped():
    backend = StubBackend({
        "classification": "LEAD",
        "confidence": 0.95,
        "reason": "Hiring.",
        "evidence_quote": "looking for a senior backend engineer",
        "budget": "$250,000",        # never stated in the post
        "company": "Globex Corp",    # never stated in the post
        "job_title": "Backend Engineer",
        "skills": ["Python"],
    })
    verdict = classify_with_llm(POST, backend)
    assert verdict.classification == "LEAD"
    assert verdict.budget is None
    assert verdict.company is None
    assert verdict.job_title == "Backend Engineer"


def test_malformed_json_does_not_crash():
    verdict = classify_with_llm(POST, StubBackend("not json at all"))
    assert verdict.classification == "UNCERTAIN"
    assert verdict.error


def test_backend_exception_is_contained():
    verdict = classify_with_llm(POST, StubBackend(None, raise_exc=RuntimeError("api down")))
    assert verdict.classification == "UNCERTAIN"
    assert verdict.error


def test_json_in_code_fence_is_parsed():
    raw = '```json\n{"classification": "NOT_LEAD", "confidence": 0.9, "reason": "seeker"}\n```'
    verdict = classify_with_llm(POST, StubBackend(raw))
    assert verdict.classification == "NOT_LEAD"


def test_confidence_is_clamped():
    backend = StubBackend({
        "classification": "NOT_LEAD", "confidence": 5.0, "reason": "x",
    })
    assert classify_with_llm(POST, backend).confidence == 1.0


def test_llm_is_not_called_when_rules_are_decisive():
    """The LLM is for ambiguity; obvious cases must not spend a request."""
    reqs = load_requirements()
    backend = StubBackend({"classification": "LEAD", "confidence": 1.0, "reason": ""})
    classify("Open to work.", reqs, llm=backend)
    assert backend.calls == 0

    classify("We are hiring a Flutter developer for our team.", reqs, llm=backend)
    assert backend.calls == 0


def test_llm_failure_falls_back_to_rules():
    reqs = load_requirements()
    backend = StubBackend(None, raise_exc=RuntimeError("down"))
    result = classify("Anyone know a good dev?", reqs, llm=backend)
    assert result.classification in ("LEAD", "NOT_LEAD")
    assert result.decided_by == "rules"
