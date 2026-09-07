"""The classifier is the core of the system, so its examples are tested first.

Every LEAD/NOT_LEAD example from the specification appears here verbatim.
These run without an LLM: the rule layer alone must get them right.
"""

import pytest

from circle_leads.classifier.lead_classifier import classify, meets_requirements
from circle_leads.config.settings import Requirements, load_requirements


@pytest.fixture
def reqs(dev_requirements):
    return dev_requirements


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


# --- Hiring phrased as a need or a referral ask -----------------------------


@pytest.mark.parametrize(
    "text",
    [
        "We need someone to build our iOS and Android app. Budget $20k, ASAP.",
        "We want someone to help with our app redesign.",
        "Anyone know a good Flutter dev?",
        "Anyone know a good mobile developer? We are rebuilding our app in Flutter.",
        "Does anybody here know a solid React Native developer?",
        "We're after someone to build our MVP.",
    ],
)
def test_need_and_referral_phrasings_are_leads(text, reqs):
    """Hiring often starts as "we need someone to..." or "anyone know a good...".

    Neither names a vacancy, and both were previously missed.
    """
    assert classify(text, reqs).classification == "LEAD"


@pytest.mark.parametrize(
    "text",
    [
        "Anyone know a good tutorial for Flutter?",
        "Does anyone know how to fix this Flutter build error?",
        "Anyone know a good podcast about startups?",
        "We need someone to explain how this API works.",
    ],
)
def test_referral_pattern_does_not_catch_questions_about_things(text, reqs):
    """Asking about a tutorial or a bug is not asking for a person to hire."""
    assert classify(text, reqs).classification == "NOT_LEAD"


# --- Real phrasings from live community feeds -------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Could you assist me finding engineers, website developers for our startup?",
        "We're finding a backend developer for our team",
        "Looking to bring on a developer for our product",
        "We want to bring on board a designer",
        "Recruiting a senior engineer for our startup",
        "Need help sourcing a mobile developer",
    ],
)
def test_finding_and_bring_on_phrasings_are_leads(text, reqs):
    """"finding engineers" and "bring on a developer" are real hiring phrasings
    seen in a live job-posts feed that were previously missed."""
    assert classify(text, reqs).classification == "LEAD"


@pytest.mark.parametrize(
    "text",
    [
        "Finding good documentation for React has been hard",
        "I'm finding it difficult to learn Flutter",
        "Bring on the weekend, I need a break",
    ],
)
def test_finding_non_role_objects_are_not_leads(text, reqs):
    assert classify(text, reqs).classification == "NOT_LEAD"


# --- Title extraction for exec and software roles ---------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("We are hiring a software engineer", "Software Engineer"),
        ("Looking for a CTO for our startup", "CTO"),
        ("VP of Engineering needed", "VP Of Engineering"),
        ("Hiring a Head of Software Engineering", "Head Of Software Engineering"),
    ],
)
def test_exec_and_software_roles_are_extracted(text, expected):
    from circle_leads.classifier.extraction import extract_job_title
    assert extract_job_title(text) == expected


def test_software_engineer_lead_passes_requirements(reqs):
    result = classify("We are hiring a software engineer for our startup", reqs)
    assert result.classification == "LEAD"
    assert meets_requirements(result, reqs) is True


# --- Direct unit tests for the rule layer -----------------------------------


def test_analyze_scores_hiring_intent():
    from circle_leads.classifier import keyword_rules as kr
    r = kr.analyze("We are hiring a backend developer")
    assert r.score > 0
    assert r.hiring_matches
    assert not r.disqualifiers


def test_analyze_scores_job_seeking_negative():
    from circle_leads.classifier import keyword_rules as kr
    r = kr.analyze("I am looking for a job as a developer, open to work")
    assert r.score < 0
    assert r.seeker_matches


def test_analyze_negation_is_disqualifier():
    from circle_leads.classifier import keyword_rules as kr
    r = kr.analyze("We are not hiring developers this quarter")
    assert r.has_hard_disqualifier


def test_matched_keywords_finds_config_terms():
    from circle_leads.classifier import keyword_rules as kr
    assert "hiring" in kr.matched_keywords("we are hiring now", ["hiring", "budget"])
    assert kr.matched_keywords("nothing here", ["hiring"]) == []


def test_has_hiring_vocabulary():
    from circle_leads.classifier import keyword_rules as kr
    assert kr.has_hiring_vocabulary("we are hiring")
    assert not kr.has_hiring_vocabulary("nice weather today")
