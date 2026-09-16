"""ICP relevance classifier: rules-only decisiveness and LLM escalation
(stubbed, no network) -- mirrors tests/test_ai_classifier.py's shape."""

import json

from circle_leads.classifier.icp_relevance import (
    ICP_CONFIDENT_NO,
    ICP_CONFIDENT_YES,
    IcpVerdict,
    assess_icp_fit,
    classify_icp_fit,
    classify_with_llm,
)


class StubBackend:
    def __init__(self, payload, raise_exc=None):
        self.payload = payload
        self.raise_exc = raise_exc
        self.calls = 0

    def complete(self, system, user):
        self.calls += 1
        if self.raise_exc:
            raise self.raise_exc
        return json.dumps(self.payload) if isinstance(self.payload, dict) else self.payload


# --- Rules layer -------------------------------------------------------------


def test_startup_founder_community_scores_positive():
    a = assess_icp_fit(
        "Founders Circle", "A community for startup founders building SaaS products"
    )
    assert a.score >= ICP_CONFIDENT_YES
    assert "startup" in a.reasons


def test_hobby_community_scores_low_or_negative():
    a = assess_icp_fit("Yoga & Meditation Retreat", "Weekly yoga and mindfulness sessions")
    assert a.score <= ICP_CONFIDENT_NO


def test_goal_signal_nudges_score():
    with_goal = assess_icp_fit("Some Community", "A group for professionals",
                                goal="build-my-tech-skills")
    without_goal = assess_icp_fit("Some Community", "A group for professionals", goal=None)
    assert with_goal.score > without_goal.score
    assert "goal:build-my-tech-skills" in with_goal.reasons


def test_negative_goal_signal_pulls_score_down():
    health = assess_icp_fit("Some Community", "A group for professionals",
                             goal="improve-my-health")
    neutral = assess_icp_fit("Some Community", "A group for professionals", goal=None)
    assert health.score < neutral.score


def test_empty_input_scores_zero():
    a = assess_icp_fit(None, None)
    assert a.score == 0
    assert a.reasons == []


# --- classify_icp_fit: decisive bands skip the LLM ---------------------------


def test_confident_yes_does_not_call_llm():
    backend = StubBackend({"fit": False, "confidence": 0.9, "reason": "no"})
    result = classify_icp_fit(
        "Startup Founders Hub", "For founders and entrepreneurs building SaaS startups",
        llm=backend,
    )
    assert backend.calls == 0
    assert result.flag is True
    assert result.decided_by == "rules"


def test_confident_no_does_not_call_llm():
    backend = StubBackend({"fit": True, "confidence": 0.9, "reason": "yes"})
    result = classify_icp_fit(
        "Yoga & Meditation Retreat", "Weekly yoga, meditation and journaling circle",
        llm=backend,
    )
    assert backend.calls == 0
    assert result.flag is False
    assert result.decided_by == "rules"


def test_no_llm_available_falls_back_to_rules_for_ambiguous_case():
    result = classify_icp_fit("Generic Community", "A group of people who meet weekly", llm=None)
    assert result.decided_by == "rules"


# --- Ambiguous band escalates to the LLM -------------------------------------


def test_ambiguous_case_escalates_to_llm():
    backend = StubBackend({"fit": True, "confidence": 0.8, "reason": "Product people",
                            "signals": ["product"]})
    result = classify_icp_fit("Product People", "A group of product managers", llm=backend)
    assert backend.calls == 1
    assert result.decided_by == "llm"
    assert result.flag is True
    assert result.score == 80


def test_llm_error_falls_back_to_rules_verdict():
    backend = StubBackend(None, raise_exc=RuntimeError("api down"))
    result = classify_icp_fit("Product People", "A group of product managers", llm=backend)
    assert result.decided_by == "rules"
    assert "llm_error" in result.reasons


def test_configurable_escalation_threshold_lowers_the_yes_cutoff():
    """A lower icp_escalation_threshold should let more cases skip the LLM as
    a confident yes, mirroring lead_classifier's llm_escalation_threshold."""
    backend = StubBackend({"fit": False, "confidence": 0.9, "reason": "no"})
    result = classify_icp_fit(
        "Tech Founders", "A group for tech and startup professionals",
        llm=backend, escalation_threshold=5,
    )
    assert backend.calls == 0
    assert result.decided_by == "rules"


# --- classify_with_llm: malformed responses don't crash ----------------------


def test_malformed_json_returns_error_verdict():
    verdict = classify_with_llm("Name", "Desc", goal=None, backend=StubBackend("not json"))
    assert verdict.error


def test_backend_exception_is_contained():
    verdict = classify_with_llm(
        "Name", "Desc", goal=None, backend=StubBackend(None, raise_exc=RuntimeError("down"))
    )
    assert verdict.error


def test_confidence_is_clamped():
    backend = StubBackend({"fit": True, "confidence": 5.0, "reason": "x"})
    verdict = classify_with_llm("Name", "Desc", goal=None, backend=backend)
    assert verdict.confidence == 1.0
