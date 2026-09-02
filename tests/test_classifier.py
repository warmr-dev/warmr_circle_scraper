"""The classifier is the core of the system, so its examples are tested first.

Every LEAD/NOT_LEAD example from the specification appears here verbatim.
These run without an LLM: the rule layer alone must get them right.
"""

import pytest

from circle_leads.classifier.lead_classifier import classify, meets_requirements
from circle_leads.config.settings import Requirements, load_requirements


@pytest.fixture(scope="module")
def reqs() -> Requirements:
    return load_requirements()


# --- Specification examples: leads -----------------------------------------

SPEC_LEADS = [
    "We are looking for a backend developer.",
    "Hiring a Flutter developer.",
    "Looking for a software engineer to join our startup.",
    "Need a React developer for an upcoming project.",
    "Our company is hiring a senior Java engineer.",
    "Looking for a developer or agency to build our application.",
    "We are looking for a software engineer.",
    "Hiring a React developer.",
    "Looking for someone to build our mobile application.",
    "Need a backend engineer.",
    "Searching for a development agency.",
    "Looking for a CTO or technical cofounder.",
]

SPEC_NOT_LEADS = [
    "I am looking for a job as a software engineer.",
    "Software engineer seeking new opportunities.",
    "Looking for work as a Flutter developer.",
    "Open to work.",
    "Any jobs available for a backend developer?",
    "I'm searching for a software engineering position.",
    "I am looking for a software engineering job.",
    "Flutter developer looking for work.",
    "I'm seeking a backend developer position.",
    "Any companies hiring engineers?",
]


@pytest.mark.parametrize("text", SPEC_LEADS)
def test_spec_examples_classified_as_lead(text, reqs):
    result = classify(text, reqs)
    assert result.classification == "LEAD", (
        f"{text!r} -> {result.classification} "
        f"(score={result.rule_score}, reason={result.reason})"
    )


@pytest.mark.parametrize("text", SPEC_NOT_LEADS)
def test_spec_examples_classified_as_not_lead(text, reqs):
    result = classify(text, reqs)
    assert result.classification == "NOT_LEAD", (
        f"{text!r} -> {result.classification} "
        f"(score={result.rule_score}, reason={result.reason})"
    )


# --- The minimal pair the spec singles out ---------------------------------


def test_minimal_pair_differs_only_by_employment_object(reqs):
    """The two sentences differ by four words; the verdicts must differ."""
    lead = classify("I'm looking for a software engineer.", reqs)
    seeker = classify("I'm looking for a job as a software engineer.", reqs)
    assert lead.classification == "LEAD"
    assert seeker.classification == "NOT_LEAD"


# --- Negation and hypotheticals --------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "We are not hiring developers this quarter.",
        "The role has been filled, thanks everyone!",
        "Hiring freeze until further notice.",
        "We aren't hiring right now.",
    ],
)
def test_negation_blocks_lead(text, reqs):
    assert classify(text, reqs).classification == "NOT_LEAD"


@pytest.mark.parametrize(
    "text",
    [
        "How do you go about hiring a developer for a startup?",
        "Tips for hiring your first engineer?",
    ],
)
def test_educational_discussion_is_not_a_lead(text, reqs):
    assert classify(text, reqs).classification == "NOT_LEAD"


# --- Vendor self-promotion --------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "I'm a senior Flutter developer available for freelance projects. DM me for my portfolio.",
        "We are an agency available for hire. Check my portfolio.",
        "Backend engineer with 8 years of experience, open to work.",
    ],
)
def test_vendor_self_promotion_is_not_a_lead(text, reqs):
    assert classify(text, reqs).classification == "NOT_LEAD"


# --- Realistic longer posts -------------------------------------------------


def test_realistic_hiring_post_scores_high(reqs):
    text = (
        "We are a seed-stage startup preparing to launch and are looking for a "
        "senior backend engineer to help us build the API. Budget is $8,000 for "
        "the first milestone and we need someone to start ASAP. Remote is fine. "
        "Python and PostgreSQL experience required."
    )
    result = classify(text, reqs)
    assert result.classification == "LEAD"
    assert result.rule_score >= 75
    assert "Python" in result.extracted["skills"]
    assert "PostgreSQL" in result.extracted["skills"]
    assert result.extracted["urgency"] == "High"
    assert result.extracted["location"] == "Remote"
    assert result.extracted["budget"] is not None


def test_realistic_job_seeker_post(reqs):
    text = (
        "Hi everyone! I'm a React developer with 5 years of experience looking "
        "for a new role. I've worked with TypeScript and Node.js. "
        "Open to work, remote preferred. My portfolio is linked in my profile."
    )
    result = classify(text, reqs)
    assert result.classification == "NOT_LEAD"


def test_agency_request_is_a_lead(reqs):
    text = (
        "Our company needs an agency to rebuild our e-commerce store. "
        "Looking for recommendations, budget around $25,000."
    )
    result = classify(text, reqs)
    assert result.classification == "LEAD"
    assert result.extracted["hire_target"] == "software agency"


def test_technical_cofounder_is_a_lead(reqs):
    result = classify(
        "Looking for a technical cofounder to join our startup and build the MVP.",
        reqs,
    )
    assert result.classification == "LEAD"
    assert result.extracted["hire_target"] == "technical cofounder"


# --- Requirements filtering -------------------------------------------------


def test_confidence_threshold_filters_weak_leads(reqs):
    result = classify("We are looking for a backend developer.", reqs)
    assert meets_requirements(result, reqs) is True

    strict = reqs.model_copy(update={"minimum_confidence": 0.99})
    assert meets_requirements(result, strict) is False


def test_requirements_are_configurable_without_code_changes(reqs):
    text = "We are hiring a COBOL developer for our mainframe team."
    narrow = reqs.model_copy(
        update={"target_roles": ["Flutter Developer"], "target_skills": ["Flutter"]}
    )
    result = classify(text, narrow)
    assert result.classification == "LEAD"
    assert meets_requirements(result, narrow) is False

    widened = reqs.model_copy(
        update={"target_roles": ["COBOL Developer"], "target_skills": ["COBOL"]}
    )
    assert meets_requirements(classify(text, widened), widened) is True


def test_empty_content_is_not_a_lead(reqs):
    assert classify("", reqs).classification == "NOT_LEAD"
    assert classify("   \n  ", reqs).classification == "NOT_LEAD"


# --- Leads that name no role or technology ---------------------------------


@pytest.mark.parametrize(
    "text,expected_target",
    [
        ("Our company needs an agency to rebuild our e-commerce store. Budget $25,000.", "software agency"),
        ("Looking for a technical cofounder to build our MVP. Equity offered.", "technical cofounder"),
        ("We need a freelancer to help finish our landing page.", "freelancer"),
    ],
)
def test_engagement_requests_without_a_job_title_still_qualify(text, expected_target, reqs):
    """An agency or cofounder request is a lead even with no title or stack."""
    result = classify(text, reqs)
    assert result.classification == "LEAD"
    assert result.extracted["hire_target"] == expected_target
    assert meets_requirements(result, reqs) is True


def test_lead_with_no_extractable_signal_is_still_filtered(reqs):
    """The filter must not become a pass-through for every LEAD."""
    narrow = reqs.model_copy(
        update={"target_roles": ["Flutter Developer"], "target_skills": ["Flutter"]}
    )
    result = classify("We are hiring a COBOL developer for our mainframe team.", narrow)
    assert result.classification == "LEAD"
    assert meets_requirements(result, narrow) is False


# --- Adversarial input ------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "we are looking for" + " " * 20000 + "x",
        "looking for a" + ("\t\n " * 3000) + "developer",
        "hiring a " + "word " * 3000 + "developer",
        "i am a " + "very " * 3000 + "engineer",
        "we need a " + "x " * 3000 + "developer",
        "seeking a " + "y " * 3000 + "engineer for our team",
    ],
)
def test_classifier_does_not_backtrack_on_adversarial_input(text, reqs):
    """Post text is untrusted input, so no pattern may blow up on it.

    These shapes previously took ~41s against patterns whose inner character
    class overlapped a preceding \\s+, letting the engine split whitespace runs
    exponentially many ways.
    """
    import time

    start = time.perf_counter()
    classify(text, reqs)
    assert time.perf_counter() - start < 2.0


def test_long_realistic_post_is_fast(reqs):
    import time

    text = "We are looking for a backend developer. " * 500
    start = time.perf_counter()
    result = classify(text, reqs)
    assert time.perf_counter() - start < 2.0
    assert result.classification == "LEAD"
