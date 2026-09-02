from datetime import datetime, timedelta, timezone

import pytest

from circle_leads.classifier.lead_classifier import classify
from circle_leads.config.settings import load_requirements
from circle_leads.scoring.lead_scoring import score_lead


@pytest.fixture(scope="module")
def reqs():
    return load_requirements()


def test_rich_recent_lead_scores_high(reqs):
    text = (
        "We are hiring a senior Backend Developer at Acme Corp. "
        "Python and AWS required, budget $120,000, remote. Start ASAP."
    )
    result = classify(text, reqs)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    score, priority, breakdown = score_lead(result, reqs, published_at=now)
    assert priority == "HIGH"
    assert score >= 80
    assert "hiring_intent" in breakdown
    assert "recent_post" in breakdown


def test_sparse_old_lead_scores_lower(reqs):
    result = classify("Need a backend engineer.", reqs)
    old = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=200)
    score, priority, _ = score_lead(result, reqs, published_at=old)
    assert score < 80
    assert priority in ("MEDIUM", "LOW")


def test_non_lead_scores_zero_hiring_component(reqs):
    result = classify("I am looking for a job as a software engineer.", reqs)
    score, priority, breakdown = score_lead(result, reqs)
    assert "hiring_intent" not in breakdown
    assert priority == "LOW"


def test_score_is_bounded(reqs):
    text = (
        "We are hiring a Backend Developer at Acme. Python, AWS, PostgreSQL, "
        "React, Docker, Kubernetes. Budget $200,000. Remote. ASAP."
    )
    result = classify(text, reqs)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    score, _, _ = score_lead(result, reqs, published_at=now)
    assert 0 <= score <= 100


def test_weights_are_configurable(reqs):
    result = classify("We are looking for a backend developer.", reqs)
    base, _, _ = score_lead(result, reqs)
    retuned = reqs.model_copy(
        update={"scoring": reqs.scoring.model_copy(update={"hiring_intent": 10})}
    )
    lowered, _, _ = score_lead(result, retuned)
    assert lowered < base
