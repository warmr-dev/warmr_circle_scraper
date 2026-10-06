"""Semantic commercial-demand classification with diagnostic keyword signals.

Only explicit request grammar can provide a conservative no-model fallback.
Ambiguous content and model failures remain retryable processing errors.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from circle_leads.classifier import keyword_rules
from circle_leads.classifier.ai_classifier import (
    CLASSIFIER_VERSION,
    AiVerdict,
    LlmBackend,
    classify_with_llm,
)
from circle_leads.classifier.extraction import extract_all
from circle_leads.config.settings import Requirements

logger = logging.getLogger(__name__)

RULES_VERSION = "rules-v3"

# Rule scores at or beyond these bounds are decisive on their own.
RULE_CONFIDENT_LEAD = 35
RULE_CONFIDENT_NOT_LEAD = -20


@dataclass
class ClassificationResult:
    classification: str = "NOT_LEAD"
    confidence: float = 0.0
    reason: str = ""
    decided_by: str = "rules"
    classifier_version: str = RULES_VERSION
    evidence_quote: str | None = None
    rule_score: int = 0
    rule_signals: dict[str, Any] = field(default_factory=dict)
    hiring_matches: list[str] = field(default_factory=list)
    seeker_matches: list[str] = field(default_factory=list)
    disqualifiers: list[str] = field(default_factory=list)
    extracted: dict[str, Any] = field(default_factory=dict)
    # The model's description the lead rule decided on (author_role, wants,
    # work_type, work_mode, summary). Empty when the rules decided.
    described: dict[str, str] = field(default_factory=dict)
    # Provider/schema/evidence failures schedule a durable retry.
    llm_error: str | None = None
    model: str | None = None
    demand_signal: str = "none"
    awareness: int | None = None
    supporting_excerpts: list[dict] = field(default_factory=list)

    @property
    def is_lead(self) -> bool:
        return self.classification == "LEAD"


def _rule_confidence(score: int) -> float:
    """Map a rule score onto a rough confidence, saturating at the extremes."""
    if score >= RULE_CONFIDENT_LEAD:
        return min(0.95, 0.80 + (score - RULE_CONFIDENT_LEAD) / 300)
    if score <= RULE_CONFIDENT_NOT_LEAD:
        return min(0.95, 0.75 + abs(score - RULE_CONFIDENT_NOT_LEAD) / 200)
    return 0.5 + abs(score) / 300


def _first_sentence_with_intent(text: str, matches: list[str]) -> str | None:
    """Pick a verbatim sentence to show the reviewer as evidence."""
    if not matches:
        return None
    for match in re.finditer(r"[^.\n]+[.]?", text):
        sentence = match.group().strip()
        if keyword_rules.has_hiring_vocabulary(sentence):
            return sentence[:300]
    return None


def classify(
    text: str,
    requirements: Requirements,
    *,
    llm: LlmBackend | None = None,
    model_name: str | None = None,
    context: list[dict] | None = None,
    current_metadata: dict | None = None,
) -> ClassificationResult:
    """Classify one piece of content as LEAD or NOT_LEAD."""
    text = (text or "").strip()
    if not text:
        return ClassificationResult(
            classification="NOT_LEAD", confidence=1.0, reason="Empty content."
        )

    rules = keyword_rules.analyze(text)
    result = ClassificationResult(
        rule_score=rules.score,
        rule_signals=rules.signals,
        hiring_matches=rules.hiring_matches,
        seeker_matches=rules.seeker_matches,
        disqualifiers=rules.disqualifiers,
    )

    # Scores, negation, seeker vocabulary and configured exclusions are only
    # diagnostics. Mixed posts must reach semantic evaluation across categories.
    if requirements.keywords.exclude:
        result.rule_signals["config_exclude_matches"] = keyword_rules.matched_keywords(
            text, requirements.keywords.exclude
        )
    # Preserve the existing exact fraud-template exclusion, not broad keywords.
    if "scam_remote_partner" in rules.disqualifiers:
        result.classification = "NOT_LEAD"
        result.reason = "Excluded recruitment scam template."
        result.confidence = 0.99
        result.evidence_quote = text
        result.supporting_excerpts = [{"source_id": "current", "quote": text}]
        return result
    if llm is not None:
        verdict = classify_with_llm(text, llm, model_name=model_name, context=context, current_metadata=current_metadata)
        if verdict.classification in ("LEAD", "NOT_LEAD") and not verdict.error:
            return _from_ai(verdict, rules, text, requirements, result)
        result.classification = "UNCERTAIN"
        result.llm_error = (verdict.error or verdict.reason or "no verdict")[:200]
        result.reason = result.llm_error
        result.model = model_name or verdict.model or getattr(llm, "model", None)
        result.decided_by = "llm"
        result.classifier_version = CLASSIFIER_VERSION
        return result

    lead_cutoff = min(RULE_CONFIDENT_LEAD, requirements.llm_escalation_threshold)
    # Conservative explicit-request fallback. Other messages require semantic
    # evaluation rather than becoming permanent keyword-based negatives.
    hiring = rules.score >= lead_cutoff
    commercial = keyword_rules.requests_commercial_work(text)
    result.classification = "LEAD" if hiring and commercial and not rules.disqualifiers and not set(rules.seeker_matches).intersection({"first_person_seeking_employment", "seeking_opportunities_self", "open_to_work", "taking_on_clients", "available_for_work"}) else "UNCERTAIN"
    result.confidence = _rule_confidence(rules.score)
    result.decided_by = "rules"
    result.classifier_version = RULES_VERSION
    if result.classification == "LEAD":
        result.reason = f"Hiring intent detected: {', '.join(rules.hiring_matches[:3])}."
        result.evidence_quote = _first_sentence_with_intent(text, rules.hiring_matches)
        result.extracted = extract_all(text, requirements.target_skills)
    elif hiring:
        result.reason = (
            f"Hiring intent ({', '.join(rules.hiring_matches[:3])}), "
            "but semantic commercial-demand evaluation is required."
        )
    else:
        result.reason = (
            f"No sufficient hiring intent (rule score {rules.score})."
            if not rules.seeker_matches
            else f"Job-seeking signals outweigh hiring signals (score {rules.score})."
        )
    if result.is_lead:
        result.demand_signal = "explicit_demand"
        result.awareness = 5
        result.supporting_excerpts = [{"source_id": "current", "quote": result.evidence_quote}]
    else:
        result.llm_error = "semantic_backend_required"
    return result


def _from_ai(
    verdict: AiVerdict,
    rules: keyword_rules.RuleResult,
    text: str,
    requirements: Requirements,
    result: ClassificationResult,
) -> ClassificationResult:
    result.classification = verdict.classification
    result.confidence = verdict.confidence
    result.reason = verdict.reason
    result.decided_by = "llm"
    result.classifier_version = CLASSIFIER_VERSION
    result.evidence_quote = verdict.evidence_quote
    result.described = dict(verdict.described)
    result.model = verdict.model
    result.demand_signal = verdict.demand_signal
    result.awareness = verdict.awareness
    result.supporting_excerpts = verdict.supporting_excerpts
    if verdict.disqualifiers:
        result.disqualifiers = list(
            dict.fromkeys(result.disqualifiers + verdict.disqualifiers)
        )

    if verdict.classification == "LEAD":
        # Fall back to pattern extraction for anything the model left null.
        fallback = extract_all(text, requirements.target_skills)
        skills = verdict.skills or fallback.get("skills") or []
        result.extracted = {
            "job_title": verdict.job_title or fallback.get("job_title"),
            "skills": list(dict.fromkeys(skills)),
            "employment_type": verdict.employment_type or fallback.get("employment_type"),
            "hire_target": verdict.hire_target or fallback.get("hire_target"),
            "company": verdict.company or fallback.get("company"),
            "budget": verdict.budget or fallback.get("budget"),
            "location": verdict.location or fallback.get("location"),
            "urgency": verdict.urgency or fallback.get("urgency"),
        }
    return result


def meets_requirements(
    result: ClassificationResult, requirements: Requirements
) -> bool:
    """Apply the user's configured filters to a classified item."""
    if not result.is_lead:
        return False
    if result.confidence < requirements.minimum_confidence:
        return False
    # Target roles/skills are routing and score metadata, not admission gates.
    return True
