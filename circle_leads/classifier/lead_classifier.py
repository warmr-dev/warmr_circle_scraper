"""Orchestrates the classification layers.

Flow:
  rules -> confident? use it.
         -> uncertain and an LLM is available? escalate.
         -> otherwise fall back to the rule verdict and mark it for review.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from circle_leads.classifier import keyword_rules
from circle_leads.classifier.ai_classifier import (
    CLASSIFIER_VERSION,
    AiVerdict,
    LlmBackend,
    classify_with_llm,
)
from circle_leads.classifier.extraction import extract_all, titles_match
from circle_leads.config.settings import Requirements

logger = logging.getLogger(__name__)

RULES_VERSION = "rules-v1"

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
    for sentence in [s.strip() for s in text.replace("\n", ". ").split(".") if s.strip()]:
        if keyword_rules.has_hiring_vocabulary(sentence):
            return sentence[:300]
    return None


def classify(
    text: str,
    requirements: Requirements,
    *,
    llm: LlmBackend | None = None,
    model_name: str | None = None,
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

    # A hard disqualifier ("not hiring", "role filled") ends it immediately.
    if rules.has_hard_disqualifier:
        result.classification = "NOT_LEAD"
        result.confidence = 0.9
        result.reason = f"Disqualified by: {', '.join(rules.disqualifiers)}."
        result.extracted = {}
        return result

    # An explicit job-seeker signal with no competing hiring signal is decisive.
    if rules.seeker_matches and not rules.hiring_matches:
        result.classification = "NOT_LEAD"
        result.confidence = 0.92
        result.reason = (
            "Author is seeking work for themselves "
            f"({', '.join(rules.seeker_matches[:3])})."
        )
        return result

    # Config-driven keyword lists act as an extra, user-controlled signal on
    # top of the pattern rules.
    if requirements.keywords.exclude and keyword_rules.matched_keywords(
        text, requirements.keywords.exclude
    ):
        matched = keyword_rules.matched_keywords(text, requirements.keywords.exclude)
        result.seeker_matches.append(f"config_exclude:{matched[0]}")
        result.rule_score -= 30
        rules.score -= 30

    lead_cutoff = min(RULE_CONFIDENT_LEAD, requirements.llm_escalation_threshold)
    needs_llm = not (
        rules.score >= lead_cutoff or rules.score <= RULE_CONFIDENT_NOT_LEAD
    )

    if needs_llm and llm is not None:
        verdict = classify_with_llm(text, llm, model_name=model_name)
        if verdict.classification in ("LEAD", "NOT_LEAD") and not verdict.error:
            return _from_ai(verdict, rules, text, requirements, result)
        logger.debug("LLM inconclusive; falling back to rules")

    # Rule-only verdict.
    result.classification = "LEAD" if rules.score >= lead_cutoff else "NOT_LEAD"
    result.confidence = _rule_confidence(rules.score)
    result.decided_by = "rules"
    result.classifier_version = RULES_VERSION
    if result.classification == "LEAD":
        result.reason = f"Hiring intent detected: {', '.join(rules.hiring_matches[:3])}."
        result.evidence_quote = _first_sentence_with_intent(text, rules.hiring_matches)
        result.extracted = extract_all(text, requirements.target_skills)
    else:
        result.reason = (
            f"No sufficient hiring intent (rule score {rules.score})."
            if not rules.seeker_matches
            else f"Job-seeking signals outweigh hiring signals (score {rules.score})."
        )
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


# Engagement types that are hiring intent in their own right, independent of
# any named role or technology.
NON_ROLE_HIRE_TARGETS = {
    "software agency",
    "freelancer",
    "contractor",
    "technical cofounder",
}


def meets_requirements(
    result: ClassificationResult, requirements: Requirements
) -> bool:
    """Apply the user's configured filters to a classified item."""
    if not result.is_lead:
        return False
    if result.confidence < requirements.minimum_confidence:
        return False
    if not requirements.target_roles and not requirements.target_skills:
        return True

    title = (result.extracted.get("job_title") or "").lower()
    skills = [s.lower() for s in (result.extracted.get("skills") or [])]

    role_match = bool(title) and any(
        titles_match(title, r) for r in requirements.roles_lower
    )
    skill_match = any(s in requirements.skills_lower for s in skills)

    # A request for an agency, contractor, or technical cofounder often names
    # no job title and no stack ("we need an agency to rebuild our store").
    # That is still a hiring lead, so match the engagement type against the
    # configured roles too rather than dropping it for lack of a title.
    hire_target = (result.extracted.get("hire_target") or "").lower()
    target_match = bool(hire_target) and (
        any(titles_match(hire_target, r) for r in requirements.roles_lower)
        or hire_target in NON_ROLE_HIRE_TARGETS
    )

    # A lead qualifies on any axis: requiring all of them would drop good leads
    # that name a role without listing a stack. An unextractable title is not
    # a match, so a lead with no signal at all is filtered out rather than
    # passing vacuously.
    return role_match or skill_match or target_match
