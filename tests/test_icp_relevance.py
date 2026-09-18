"""ICP relevance classifier: rules-only decisiveness and LLM escalation
(stubbed, no network) -- mirrors tests/test_ai_classifier.py's shape."""

import json

from circle_leads.classifier.icp_relevance import (
    ICP_CONFIDENT_NO,
    ICP_CONFIDENT_YES,
    ICP_MIN_LLM_CONFIDENCE,
    LLM_FIT_NEEDS_REVIEW,
    NO_METADATA_REASON,
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


class LlmWasCalled(BaseException):
    """Deliberately not an Exception: classify_with_llm catches Exception, so an
    ordinary assertion inside a backend would be swallowed and the test would
    pass while the LLM was in fact reached."""


class ExplodingBackend:
    def __init__(self):
        self.calls = 0

    def complete(self, system, user):
        self.calls += 1
        raise LlmWasCalled(f"the LLM must not be reached; prompt was: {user!r}")


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
    result = classify_icp_fit("Product People", "A group of product managers",
                              llm=backend, llm_may_flag=True)
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


# --- Empty-metadata guard ----------------------------------------------------


def test_empty_metadata_row_never_reaches_the_llm():
    """~80% of prod rows have neither name nor description; escalating them sent
    a "(none) / (none) / (none)" prompt that the model answered anyway."""
    backend = ExplodingBackend()
    result = classify_icp_fit(None, None, goal=None, llm=backend, escalation_threshold=20)
    assert backend.calls == 0
    assert result.decided_by == "rules"


def test_empty_metadata_row_is_flagged_false_with_a_distinct_marker():
    result = classify_icp_fit(None, None, goal=None, llm=ExplodingBackend())
    assert result.flag is False
    assert result.score == 0.0
    assert result.reasons == [NO_METADATA_REASON]


def test_whitespace_only_metadata_counts_as_empty():
    result = classify_icp_fit("   ", "\n\t ", tags=[" "], goal=None, llm=ExplodingBackend())
    assert result.reasons == [NO_METADATA_REASON]
    assert result.flag is False


def test_whitespace_only_goal_does_not_bypass_the_empty_guard():
    """`not goal` is true for None but not for "   ", so a blank directory goal
    used to carry an otherwise-empty row into the prompt: name "(none)",
    description "(none)", goal "   " -- and the model answered anyway (observed
    fit at 0.9)."""
    backend = ExplodingBackend()
    result = classify_icp_fit(None, None, goal="   ", llm=backend)
    assert backend.calls == 0
    assert result.reasons == [NO_METADATA_REASON]
    assert result.flag is False


def test_a_padded_goal_still_matches_its_category():
    """Same normalisation from the other side: the directory value is scraped
    text, so the surrounding whitespace must not cost the row its signal."""
    padded = assess_icp_fit(None, None, goal="  build-my-tech-skills  ")
    assert padded.reasons == ["goal:build-my-tech-skills"]
    assert padded.score == assess_icp_fit(None, None, goal="build-my-tech-skills").score


def test_a_genuine_rejection_is_not_marked_no_metadata():
    """The marker has to separate "never judged" from "judged and rejected"."""
    result = classify_icp_fit("Yoga & Meditation Retreat", "Weekly yoga and journaling")
    assert result.flag is False
    assert NO_METADATA_REASON not in result.reasons


def test_goal_only_row_still_scores():
    """No name or description, but a directory goal is real signal (+5..+20),
    so the row must still go through scoring rather than be guarded out."""
    backend = StubBackend({"fit": True, "confidence": 0.9, "reason": "tech skills"})
    result = classify_icp_fit(None, None, goal="build-my-tech-skills", llm=backend)
    assert NO_METADATA_REASON not in result.reasons
    assert "goal:build-my-tech-skills" in result.reasons
    assert backend.calls == 1


def test_goal_only_row_can_be_decided_by_rules_under_a_low_threshold():
    backend = ExplodingBackend()
    result = classify_icp_fit(None, None, goal="build-my-tech-skills",
                              llm=backend, escalation_threshold=20)
    assert backend.calls == 0
    assert result.decided_by == "rules"
    assert result.flag is True


# --- LLM confidence floor ----------------------------------------------------


def test_low_confidence_llm_fit_does_not_flag():
    backend = StubBackend({"fit": True, "confidence": 0.1, "reason": "maybe product people"})
    result = classify_icp_fit("Product People", "A group of product managers", llm=backend)
    assert result.decided_by == "llm"
    assert result.flag is False
    assert "low_llm_confidence" in result.reasons


def test_high_confidence_llm_fit_clears_the_floor():
    backend = StubBackend({"fit": True, "confidence": 0.9, "reason": "Founders",
                            "signals": ["founders"]})
    result = classify_icp_fit("Product People", "A group of product managers",
                              llm=backend, llm_may_flag=True)
    assert result.decided_by == "llm"
    assert result.flag is True
    assert "low_llm_confidence" not in result.reasons


def test_confidence_floor_is_inclusive_at_the_constant():
    backend = StubBackend({"fit": True, "confidence": ICP_MIN_LLM_CONFIDENCE, "reason": "x"})
    result = classify_icp_fit("Product People", "A group of product managers",
                              llm=backend, llm_may_flag=True)
    assert result.flag is True


def test_confidence_floor_is_overridable_by_the_caller():
    backend = StubBackend({"fit": True, "confidence": 0.2, "reason": "x"})
    result = classify_icp_fit("Product People", "A group of product managers",
                              llm=backend, min_llm_confidence=0.1, llm_may_flag=True)
    assert result.flag is True


def test_confident_llm_no_is_stored_as_a_low_fit_score():
    # icp_score is "how well it fits" on every path; a 0.9-confident "no" used
    # to be stored as 90 and outranked real fits in the join queue.
    backend = StubBackend({"fit": False, "confidence": 0.9, "reason": "herbalism"})
    result = classify_icp_fit("Product People", "A group of product managers", llm=backend)
    assert result.decided_by == "llm"
    assert result.flag is False
    assert result.score == 10


def test_llm_no_read_as_fit_probability_also_stays_low():
    # The same model also answers a "no" as "0.1 chance it fits"; inverting
    # that blindly would store 90. Either reading of a "no" must land low.
    backend = StubBackend({"fit": False, "confidence": 0.1, "reason": "unclear"})
    result = classify_icp_fit("Product People", "A group of product managers", llm=backend)
    assert result.score == 10


def test_confident_llm_fit_keeps_its_confidence_as_the_score():
    backend = StubBackend({"fit": True, "confidence": 0.8, "reason": "builders"})
    result = classify_icp_fit("Product People", "A group of product managers",
                              llm=backend, llm_may_flag=True)
    assert result.score == 80


# --- An LLM fit does not reach the auto-join queue on its own ----------------


def test_confident_llm_fit_does_not_flag_without_the_opt_in():
    """icp_flag is the only gate before join/joiner.py drives a browser on the
    user's real Circle account. A model's yes on a two-line listing gets
    recorded and marked for review; it does not enqueue a join."""
    backend = StubBackend({"fit": True, "confidence": 0.95, "reason": "Founders",
                            "signals": ["founders"]})
    result = classify_icp_fit("Product People", "A group of product managers", llm=backend)
    assert result.decided_by == "llm"
    assert result.flag is False
    assert LLM_FIT_NEEDS_REVIEW in result.reasons
    # The verdict itself is preserved, so a reviewer (or an opt-in re-run) has
    # something to act on -- this is "not yet", not "no".
    assert result.score == 95


def test_llm_fit_flags_when_the_caller_opts_in():
    backend = StubBackend({"fit": True, "confidence": 0.95, "reason": "Founders"})
    result = classify_icp_fit("Product People", "A group of product managers",
                              llm=backend, llm_may_flag=True)
    assert result.flag is True
    assert LLM_FIT_NEEDS_REVIEW not in result.reasons


def test_rules_decided_fit_flags_without_any_opt_in():
    """The opt-in is about LLM opinions only: deterministic rule signals still
    flag on their own, or the whole funnel would stop."""
    result = classify_icp_fit(
        "Startup Founders Hub", "For founders and entrepreneurs building SaaS startups",
    )
    assert result.decided_by == "rules"
    assert result.flag is True
    assert LLM_FIT_NEEDS_REVIEW not in result.reasons


def test_low_confidence_llm_fit_is_not_also_marked_for_review():
    """Below the confidence floor the verdict is not worth a human's time
    either; one marker per row keeps the reason list readable."""
    backend = StubBackend({"fit": True, "confidence": 0.1, "reason": "maybe"})
    result = classify_icp_fit("Product People", "A group of product managers", llm=backend)
    assert "low_llm_confidence" in result.reasons
    assert LLM_FIT_NEEDS_REVIEW not in result.reasons


def test_llm_non_fit_is_not_marked_for_review():
    backend = StubBackend({"fit": False, "confidence": 0.9, "reason": "hobby group"})
    result = classify_icp_fit("Product People", "A group of product managers", llm=backend)
    assert result.flag is False
    assert LLM_FIT_NEEDS_REVIEW not in result.reasons


def test_high_confidence_non_fit_still_does_not_flag():
    backend = StubBackend({"fit": False, "confidence": 0.95, "reason": "hobby group"})
    result = classify_icp_fit("Product People", "A group of product managers", llm=backend)
    assert result.flag is False
