"""The lead rule (decided 2026-09-24): the model describes, code decides.

A post is a lead when a buyer wants software work done by someone else -- as
a service, a hire or a cofounder. A hire for anything else (an office seat, a
Google Ads account, a CEO) is not, and neither is a seller, a job seeker or an
announcement. The model never says LEAD itself; LEAD_RULE reads its answers.
"""

import itertools
import json
from datetime import datetime

import pytest
from sqlalchemy import select

from circle_leads.classifier.ai_classifier import (
    CLASSIFIER_VERSION,
    DESCRIPTION_VALUES,
    LEAD_RULE,
    SYSTEM_PROMPT,
    classify_with_llm,
    is_lead,
    lead_reason,
    read_description,
)
from circle_leads.classifier.lead_classifier import classify, meets_requirements
from circle_leads.storage.database import Database, get_or_create_community, upsert_post
from circle_leads.storage.models import Lead


class StubBackend:
    """Returns a canned model reply and records what it was sent."""

    model = "stub-model"

    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def complete(self, system, user):
        self.calls.append((system, user))
        return json.dumps(self.payload) if isinstance(self.payload, dict) else self.payload


def reply(author_role="buyer", wants="service", work_type="software", **extra):
    return {"summary": "Someone asks for something.", "author_role": author_role,
            "wants": wants, "work_type": work_type, "work_mode": "remote",
            "confidence": 0.9, **extra}


def desc(author_role, wants, work_type, work_mode="unknown"):
    return {"author_role": author_role, "wants": wants, "work_type": work_type,
            "work_mode": work_mode}


SNOWFLAKE = ("Education Migration To Snowflake. I am working with an organization "
             "looking to move off their current data warehouse onto Snowflake. "
             "It'll likely be a 5-6 month project. Technical Skills Required: "
             "Snowflake, SQL, dbt. Weekly Commitment In Hours: 30-35.")
GOOGLE_ADS = ("Need a Google Ads Expert in Lead Gen of High Income Clients with "
              "proven results. I run a marketing agency and I have a client in an "
              "expensive industry. Contract, remote.")


# --- The rule table ----------------------------------------------------------


def test_the_rule_reads_only_known_fields_and_values():
    """A typo in the table ("sofware") would silently make nothing a lead."""
    for key, allowed in LEAD_RULE.items():
        assert key in DESCRIPTION_VALUES
        assert allowed <= set(DESCRIPTION_VALUES[key])


def test_exactly_three_descriptions_are_leads():
    keys = ("author_role", "wants", "work_type")
    leads = sorted(
        combo for combo in itertools.product(*(DESCRIPTION_VALUES[k] for k in keys))
        if is_lead(dict(zip(keys, combo)))
    )
    assert leads == [
        ("buyer", "cofounder", "software"),
        ("buyer", "employee", "software"),
        ("buyer", "service", "software"),
    ]


@pytest.mark.parametrize("described,expected", [
    # The kind of help sought does not matter...
    (desc("buyer", "service", "software"), True),       # contract gig, agency, RFP
    (desc("buyer", "employee", "software"), True),      # a developer on staff
    (desc("buyer", "cofounder", "software"), True),     # technical cofounder / CTO
    # ...and neither does where the work happens.
    (desc("buyer", "employee", "software", "onsite"), True),
    # Hires for work we do not sell.
    (desc("buyer", "employee", "admin"), False),        # office manager, EA
    (desc("buyer", "service", "marketing"), False),     # Google Ads, SEO
    (desc("buyer", "employee", "sales"), False),        # SDR, lead generation
    (desc("buyer", "employee", "content"), False),      # video editor
    (desc("buyer", "service", "design"), False),        # logo, brand identity
    (desc("buyer", "employee", "finance_legal"), False),
    (desc("buyer", "employee", "other"), False),        # a CEO
    (desc("buyer", "cofounder", "other"), False),       # a business partner
    # Not a buyer at all.
    (desc("seller", "service", "software"), False),     # "we're a dev agency"
    (desc("job_seeker", "employee", "software"), False),
    (desc("other", "nothing", "software"), False),      # an article on hiring devs
    (desc("buyer", "nothing", "software"), False),      # advice, or role filled
])
def test_lead_rule(described, expected):
    assert is_lead(described) is expected


# --- The reason --------------------------------------------------------------


def test_a_lead_reason_names_the_engagement_and_the_role():
    assert (lead_reason(desc("buyer", "employee", "software"), "Data Engineer")
            == "Lead: a buyer wants to hire someone for software work (Data Engineer).")
    assert lead_reason(desc("buyer", "service", "software")) == (
        "Lead: a buyer wants a contractor, freelancer or agency for software work.")


@pytest.mark.parametrize("described,words", [
    (desc("seller", "service", "software"), "selling their own services"),
    (desc("job_seeker", "employee", "software"), "looking for work"),
    (desc("other", "nothing", "other"), "nobody is buying work"),
    (desc("buyer", "nothing", "software"), "does not ask anyone to do work"),
    (desc("buyer", "employee", "admin"), "admin or office work, not software"),
    (desc("buyer", "service", "marketing"), "marketing, not software"),
])
def test_a_not_lead_reason_names_the_first_failed_condition(described, words):
    reason = lead_reason(described, "Office Manager")
    assert reason.startswith("Not a lead: ") and reason.endswith(".")
    assert words in reason


# --- Reading the model's reply -----------------------------------------------


def test_the_prompt_offers_every_value_the_rule_can_read():
    for values in DESCRIPTION_VALUES.values():
        for value in values:
            assert f'"{value}"' in SYSTEM_PROMPT


def test_answers_are_normalised():
    described = read_description({"author_role": " Buyer ", "wants": "Service",
                                  "work_type": "finance/legal", "work_mode": "On-site"})
    # "On-site" is not in the vocabulary; work_mode is descriptive only.
    assert described == desc("buyer", "service", "finance_legal", "unknown")
    assert read_description({"author_role": "job seeker", "wants": "nothing",
                             "work_type": "other"})["author_role"] == "job_seeker"


@pytest.mark.parametrize("data", [
    {"wants": "service", "work_type": "software"},                          # missing
    {"author_role": "recruiter", "wants": "service", "work_type": "software"},
    {"author_role": "buyer", "wants": "service", "work_type": "data"},
    {"classification": "LEAD", "confidence": 1.0},                           # old format
])
def test_an_unusable_description_is_rejected(data):
    assert read_description(data) is None


def test_code_decides_not_the_model():
    """A "classification" in the reply is ignored: the description decides."""
    says_lead = StubBackend(reply(work_type="admin", classification="LEAD",
                                  evidence_quote="looking to move off"))
    assert classify_with_llm(SNOWFLAKE, says_lead).classification == "NOT_LEAD"

    says_not = StubBackend(reply(classification="NOT_LEAD",
                                 evidence_quote="looking to move off their current data warehouse"))
    assert classify_with_llm(SNOWFLAKE, says_not).classification == "LEAD"


def test_the_model_is_sent_the_describe_prompt_and_the_post():
    backend = StubBackend(reply(evidence_quote="onto Snowflake"))
    classify_with_llm(SNOWFLAKE, backend)
    (system, user), = backend.calls
    assert system == SYSTEM_PROMPT
    assert SNOWFLAKE in user


def test_a_verdict_carries_the_description_and_a_code_written_reason():
    backend = StubBackend(reply(
        reason="The model's own words, which are not stored.",
        summary="An organisation wants a contractor to migrate its warehouse to Snowflake.",
        evidence_quote="looking to move off their current data warehouse onto Snowflake",
        job_title="Snowflake Migration Consultant",
    ))
    verdict = classify_with_llm(SNOWFLAKE, backend, model_name="stub-model")
    assert verdict.classification == "LEAD"
    assert verdict.reason == ("Lead: a buyer wants a contractor, freelancer or agency "
                              "for software work (Snowflake Migration Consultant).")
    assert verdict.described == {
        "author_role": "buyer", "wants": "service", "work_type": "software",
        "work_mode": "remote",
        "summary": "An organisation wants a contractor to migrate its warehouse to Snowflake.",
    }


def test_a_non_software_hire_is_not_a_lead_even_when_it_is_a_hire():
    backend = StubBackend(reply(wants="service", work_type="marketing",
                                evidence_quote="Need a Google Ads Expert"))
    verdict = classify_with_llm(GOOGLE_ADS, backend)
    assert verdict.classification == "NOT_LEAD"
    assert verdict.reason == "Not a lead: the work wanted is marketing, not software."


def test_an_unusable_reply_is_no_verdict_and_the_rules_hold_it(dev_requirements):
    backend = StubBackend({"classification": "LEAD", "confidence": 0.99,
                           "evidence_quote": "onto Snowflake"})
    verdict = classify_with_llm(SNOWFLAKE, backend)
    assert verdict.classification == "UNCERTAIN" and verdict.error

    result = classify(SNOWFLAKE, dev_requirements, llm=backend)
    assert result.decided_by == "rules"
    assert result.llm_error   # callers hold this verdict, not act on it


# --- Through classify() and the requirements filter ---------------------------


def test_a_described_software_hire_passes_the_role_and_skill_filter(dev_requirements):
    # The configured roles and skills are matched exactly. A full-time Go hire
    # names neither, and the old filter dropped it after the rule had filed it.
    narrow = dev_requirements.model_copy(
        update={"target_roles": ["Flutter Developer"], "target_skills": ["Flutter"]})
    text = "We are hiring a Senior Go Engineer to join our platform team full-time."
    backend = StubBackend(reply(
        wants="employee", evidence_quote="We are hiring a Senior Go Engineer",
        job_title="Senior Go Engineer", skills=["Go", "gRPC"],
        hire_target="full-time employee"))
    result = classify(text, narrow, llm=backend)
    assert result.classification == "LEAD" and result.decided_by == "llm"
    assert result.described["work_type"] == "software"
    assert meets_requirements(result, narrow) is True

    # The confidence floor still applies.
    strict = narrow.model_copy(update={"minimum_confidence": 0.95})
    assert meets_requirements(result, strict) is False


def test_the_model_decides_what_the_rules_cannot_see(dev_requirements):
    """Rules alone no longer file "Sigma Experts Wanted"; the model still can."""
    text = "Sigma Experts Wanted. We have a few projects in the pipeline."
    assert classify(text, dev_requirements).classification == "NOT_LEAD"
    backend = StubBackend(reply(evidence_quote="Sigma Experts Wanted"))
    assert classify(text, dev_requirements, llm=backend).classification == "LEAD"


# --- The rules alone -----------------------------------------------------------


@pytest.mark.parametrize("text", [
    GOOGLE_ADS,
    "Job Opportunity: Venture Studio CEO. We are looking for a CEO to run one of "
    "our new venture studio companies. Apply below!",
    "Looking for a B2B Lead-Gen Specialist. Full-time, remote. I have one open "
    "position in my agency and need someone that can grow with us.",
    "We are hiring a Global Events Manager to lead our flagship Roadshow "
    "program. Remote job, apply here.",
])
def test_rules_alone_do_not_file_a_non_software_hire(text, dev_requirements):
    # Each reads as a confident hire to the patterns; none asks for software.
    result = classify(text, dev_requirements)
    assert result.rule_score >= 35
    assert result.classification == "NOT_LEAD"
    assert "no software work named" in result.reason


# --- Storage -------------------------------------------------------------------


@pytest.fixture
def db(tmp_path):
    return Database(f"sqlite:///{tmp_path}/leadrule.db")


def _stub_pipeline(monkeypatch, module, payload):
    pushed = []

    def fake_push(_session, ids, **_kw):
        from circle_leads.export.vini_ingest import PushResult
        pushed.extend(ids)
        return PushResult()

    monkeypatch.setattr(module, "push_leads_by_ids", fake_push)
    monkeypatch.setattr(module, "make_backend", lambda: StubBackend(payload))
    return pushed


def test_triage_stores_the_description_with_the_lead(db, dev_requirements, monkeypatch):
    import circle_leads.triage.pipeline as tp

    pushed = _stub_pipeline(monkeypatch, tp, reply(
        evidence_quote="looking to move off their current data warehouse onto Snowflake",
        job_title="Snowflake Consultant"))
    tp.triage_records(
        db, [{"content": SNOWFLAKE, "published_at": datetime(2026, 9, 20),
              "author": {"display_name": "Ben"}}],
        dev_requirements, community="tfa", use_llm=True)

    with db.session() as s:
        lead = s.scalar(select(Lead))
        assert lead.classification == "LEAD"
        assert (lead.decided_by, lead.classifier_version) == ("llm", CLASSIFIER_VERSION)
        assert lead.reason.startswith("Lead: a buyer wants")
        assert lead.score_breakdown["described"]["work_type"] == "software"
        assert lead.score_breakdown["described"]["author_role"] == "buyer"
        assert "hiring_intent" in lead.score_breakdown     # the score parts stay
        assert pushed == [lead.id]


def test_triage_files_nothing_for_a_non_software_hire(db, dev_requirements, monkeypatch):
    import circle_leads.triage.pipeline as tp

    pushed = _stub_pipeline(monkeypatch, tp, reply(
        work_type="marketing", evidence_quote="Need a Google Ads Expert"))
    res = tp.triage_records(
        db, [{"content": GOOGLE_ADS, "published_at": datetime(2026, 9, 20),
              "author": {"display_name": "Estefano"}}],
        dev_requirements, community="py", use_llm=True)

    assert res.not_leads == 1
    with db.session() as s:
        assert s.scalar(select(Lead)) is None
    assert pushed == []


def test_classify_pending_stores_the_description_with_the_lead(db, dev_requirements,
                                                               monkeypatch):
    import circle_leads.pipeline as pl

    _stub_pipeline(monkeypatch, pl, reply(
        wants="employee", evidence_quote="onto Snowflake"))
    with db.session() as s:
        c = get_or_create_community(s, slug="tfa", url="https://tfa.circle.so")
        upsert_post(s, community_id=c.id, record={
            "source_content_id": "p1", "content": SNOWFLAKE,
            "published_at": datetime(2026, 9, 20)})

    stats = pl.classify_pending(db, dev_requirements, use_llm=True)
    assert stats["leads"] == 1
    with db.session() as s:
        lead = s.scalar(select(Lead))
        assert lead.score_breakdown["described"]["wants"] == "employee"
