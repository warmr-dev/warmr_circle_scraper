"""Job-post-headline grammar: hires posted as headlines must classify as LEAD,
without flipping job-seekers who use the same words."""

from __future__ import annotations

import pytest

from circle_leads.config.settings import Requirements
from circle_leads.classifier.lead_classifier import classify


@pytest.fixture
def reqs():
    return Requirements()


@pytest.mark.parametrize("text", [
    "Senior Data/Analytics Engineer (Contract)",
    "2x DevOps/Platform Engineers needed for our platform",
    "Snowflake Developer Contract Opportunity - Dublin (Hybrid)",
])
def test_hiring_headlines_are_leads(reqs, text):
    assert classify(text, reqs, llm=None).classification == "LEAD"


@pytest.mark.parametrize("text", [
    "Snowflake, dbt, Sigma freelancers wanted for a 3-month build",
    "Sigma Experts Wanted",
])
def test_tool_only_headlines_are_left_to_the_model(reqs, text):
    # The headline grammar still reads a hire here, so a model is asked. The
    # rules alone cannot tell Sigma from a Google Ads account, and since
    # 2026-09-24 they file only hires that name software work.
    result = classify(text, reqs, llm=None)
    assert result.rule_score >= 35
    assert result.classification == "NOT_LEAD"
    assert "no software work named" in result.reason


@pytest.mark.parametrize("text", [
    "I'm a senior data engineer looking for a contract opportunity, remote preferred.",
    "Experienced Power BI developer available for freelance work. DM me.",
    "Looking for my next contract role as a DevOps engineer.",
    "Freelance Snowflake consultant here — open to new projects.",
    "I am a developer seeking full-time positions in fintech.",
])
def test_seekers_with_same_words_stay_not_lead(reqs, text):
    # The headline patterns must not flip a job-seeker; first-person seeker
    # signals win. (Rule layer only; the LLM layer refines the ambiguous ones.)
    assert classify(text, reqs, llm=None).classification != "LEAD"
