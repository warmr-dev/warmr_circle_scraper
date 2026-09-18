"""ICP (ideal-customer-profile) relevance: would a software development company
find plausible buyers of dev/software services in this community?

Structured as a direct parallel to classifier/lead_classifier.py: a cheap rules
layer decides the clear cases on its own; only the ambiguous middle escalates to
an LLM. Reuses discovery/validate_community.py's signal tables as the base
signal (already directionally ICP-aligned -- startup/saas/tech/agency score
positive, hobby/fitness score negative) rather than duplicating them, and reuses
classifier/ai_classifier.py's backend plumbing (LlmBackend, make_backend) for the
escalation path rather than a second LLM client.

This is deliberately a *different* signal from Community.relevance_score/relevant
(the older web-search ranking heuristic in discovery/finder.py): the two are not
yet proven to agree, so they're stored in separate columns until this classifier
has a track record -- see WORKLOG.md and the P20 migration.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from circle_leads.discovery.validate_community import (
    IRRELEVANT_SIGNALS,
    RELEVANCE_SIGNALS,
    HIRING_SPACE_HINTS,
)

logger = logging.getLogger(__name__)

CLASSIFIER_VERSION = "icp-v1"
RULES_VERSION = "icp-rules-v1"

# Score at or beyond these bounds is decisive without an LLM call. Unlike
# assess_relevance()'s clamped [0, 100] result, this scoring stays unclamped
# internally so a genuinely off-topic listing (multiple negative signals) can
# score below zero and be confidently excluded -- clamping only happens when
# storing the displayed icp_score.
ICP_CONFIDENT_YES = 45
ICP_CONFIDENT_NO = -10

# An LLM "fit" below this confidence does not get to set icp_flag. The backends
# return fit=true at very low confidence on thin listings, and a flagged row is
# what someone then spends a join attempt on -- a coin flip costs more than a
# miss. Set below Requirements.minimum_confidence (0.80, the per-post lead bar)
# on purpose: this is a coarse community-level pre-filter and the join step plus
# the lead classifier both filter again downstream, so it can afford to be a
# little more inclusive than a final lead verdict.
ICP_MIN_LLM_CONFIDENCE = 0.6

# Marker stored in icp_reasons when an LLM said "fit" but nothing else did.
# icp_flag is the only gate in front of the auto-join bot (join/joiner.py's
# select_join_candidates checks icp_flag + platform + join_type + status and
# nothing else), and that bot opens a browser on the user's real Circle
# account. A model's opinion on a two-line listing is not enough to spend a
# join attempt on unsupervised, so an LLM-decided fit is recorded with this
# marker and left unflagged unless a caller explicitly opts in -- see
# ``llm_may_flag`` on classify_icp_fit.
LLM_FIT_NEEDS_REVIEW = "llm_fit_needs_review"

# Marker stored in icp_reasons when a row had nothing to judge. Without it a
# never-judged row is indistinguishable in the DB from one the rules actively
# rejected, which is exactly how ~80% of prod ended up looking "decided".
NO_METADATA_REASON = "no_metadata"

# Goal categories from the Circle discovery directory (discovery/circle_directory.py)
# that skew toward software-dev-company buyers, and ones that clearly don't.
# Directory-only signal: a plain web-search find has no `goal`, so this never
# fires for that source.
GOAL_SIGNALS: dict[str, int] = {
    "build-my-tech-skills": 20,
    "start-and-scale-my-business": 15,
    "advance-my-career": 5,
    "grow-my-brand-and-audience": 5,
}
GOAL_NEGATIVE_SIGNALS: dict[str, int] = {
    "improve-my-health": -20,
    "strengthen-my-relationships": -20,
    "pursue-new-interests": -10,
}


@dataclass
class IcpAssessment:
    score: int = 0  # unclamped; may be negative
    reasons: list[str] = field(default_factory=list)


def _haystack(name: str | None, description: str | None,
              tags: list[str] | None = None) -> str:
    """The lowercased text the rules match against. Shared with the empty-text
    guard in classify_icp_fit so the two can never disagree about what "this row
    has no metadata" means."""
    return " ".join(filter(None, [name, description, " ".join(tags or [])])).lower().strip()


def assess_icp_fit(
    name: str | None,
    description: str | None,
    *,
    tags: list[str] | None = None,
    goal: str | None = None,
) -> IcpAssessment:
    """Score a community's directory/listing metadata for ICP fit. Pure, no I/O."""
    haystack = _haystack(name, description, tags)
    # A blank goal is no goal: the stored value comes from a scraped directory
    # card, so "   " happens, and `not goal` alone lets it through as if it
    # were a real category -- past the empty-text guard and into the prompt.
    goal = (goal or "").strip() or None
    assessment = IcpAssessment()
    if not haystack and not goal:
        return assessment

    import re

    for label, (weight, terms) in RELEVANCE_SIGNALS.items():
        if any(re.search(rf"\b{re.escape(t.strip())}", haystack) for t in terms):
            assessment.score += weight
            assessment.reasons.append(label)
    for label, (weight, terms) in IRRELEVANT_SIGNALS.items():
        if any(re.search(rf"\b{re.escape(t.strip())}", haystack) for t in terms):
            assessment.score += weight
            assessment.reasons.append(f"not_{label}")
    if any(h in haystack for h in HIRING_SPACE_HINTS):
        assessment.score += 15
        assessment.reasons.append("hiring_related_space")

    if goal:
        if goal in GOAL_SIGNALS:
            assessment.score += GOAL_SIGNALS[goal]
            assessment.reasons.append(f"goal:{goal}")
        elif goal in GOAL_NEGATIVE_SIGNALS:
            assessment.score += GOAL_NEGATIVE_SIGNALS[goal]
            assessment.reasons.append(f"not_goal:{goal}")

    return assessment


@dataclass
class IcpVerdict:
    fit: bool = False
    confidence: float = 0.0
    reason: str = ""
    signals: list[str] = field(default_factory=list)
    error: str | None = None


SYSTEM_PROMPT = """\
You classify online communities for a software development company's lead
generation. The question: if this company monitored this community's posts,
would they plausibly find people asking for developers, engineers, technical
cofounders, or software agencies -- i.e. buyers of software development work?

FIT: founders, startups, SaaS builders, indie hackers, no-code/product people,
  agencies, tech-adjacent professional communities -- places where someone
  building or running a software product might ask for help building it.
FIT: communities explicitly about coding, engineering, AI/ML, or technical
  career growth, even if not business-focused -- members there sometimes need
  contractors or agencies too.
NOT FIT: hobby, fitness, health, relationship, spiritual, or purely consumer
  communities with no plausible connection to commissioning software work.
NOT FIT: communities about a specific non-tech trade or craft (notary training,
  drone piloting, print-on-demand selling) unless the description itself
  mentions building software/tech products.

Return ONLY a JSON object:
{
  "fit": true | false,
  "confidence": 0.0-1.0,
  "reason": "one sentence",
  "signals": ["short phrase", ...]
}"""


def _extract_json(raw: str) -> dict[str, Any]:
    import json
    import re

    raw = raw.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.S)
    if fence:
        raw = fence.group(1)
    else:
        start, end = raw.find("{"), raw.rfind("}")
        if start != -1 and end > start:
            raw = raw[start : end + 1]
    return json.loads(raw)


def classify_with_llm(name: str | None, description: str | None, *, goal: str | None,
                       backend, model_name: str | None = None) -> IcpVerdict:
    """Escalate one ambiguous listing to the LLM. Mirrors
    ai_classifier.classify_with_llm's shape (verify-then-trust), simplified: an
    ICP fit/no-fit call has no evidence-quote to ground, unlike a lead verdict.
    """
    user = (
        f"Community name: {name or '(none)'}\n"
        f"Description: {description or '(none)'}\n"
        f"Directory goal category: {goal or '(none)'}"
    )
    try:
        raw = backend.complete(SYSTEM_PROMPT, user)
        data = _extract_json(raw)
    except Exception as exc:
        logger.warning("ICP LLM classification failed: %s", exc.__class__.__name__)
        return IcpVerdict(error=str(exc)[:200])

    try:
        confidence = max(0.0, min(1.0, float(data.get("confidence", 0.0))))
    except (TypeError, ValueError):
        confidence = 0.0
    signals = data.get("signals") or []
    if not isinstance(signals, list):
        signals = []

    return IcpVerdict(
        fit=bool(data.get("fit")),
        confidence=confidence,
        reason=str(data.get("reason", ""))[:500],
        signals=[str(s) for s in signals][:10],
    )


@dataclass
class IcpResult:
    score: float
    flag: bool
    reasons: list[str]
    decided_by: str  # "rules" | "llm"
    classifier_version: str


def classify_icp_fit(
    name: str | None,
    description: str | None,
    *,
    tags: list[str] | None = None,
    goal: str | None = None,
    llm=None,
    model_name: str | None = None,
    escalation_threshold: int | None = None,
    min_llm_confidence: float = ICP_MIN_LLM_CONFIDENCE,
    llm_may_flag: bool = False,
) -> IcpResult:
    """Full rules -> LLM-escalation flow for one community's ICP fit.

    ``escalation_threshold`` (from Requirements.icp_escalation_threshold when
    called via pipeline.classify_icp_pending) overrides the module's default
    confident-yes cutoff, mirroring how lead_classifier.classify() lets
    llm_escalation_threshold narrow RULE_CONFIDENT_LEAD.

    ``min_llm_confidence`` is the floor an LLM "fit" has to clear before it may
    set the flag; callers tune it per run, the module default is the safe bar.

    ``llm_may_flag`` is the opt-in that lets an LLM-decided fit set icp_flag at
    all. It defaults to False because icp_flag is the *only* gate before the
    auto-join bot drives the user's real Circle account: an unattended sweep
    must be able to grow the *review* backlog but not the join queue. A human
    run (``filter-relevant --trust-llm-flags``) can turn it on. Rules-decided
    fits are unaffected -- they come from deterministic signals, not a model.
    """
    # A row with no name, no description and no directory goal has nothing to
    # judge. It must not reach the LLM: the prompt would degrade to "(none) /
    # (none) / (none)" and the model still answers -- observed returning fit at
    # 0.9 confidence on literally empty input. Rules-reject it and say why, so
    # "never judged" stays distinguishable from "judged and rejected".
    goal = (goal or "").strip() or None
    if not _haystack(name, description, tags) and not goal:
        return IcpResult(
            score=0.0,
            flag=False,
            reasons=[NO_METADATA_REASON],
            decided_by="rules",
            classifier_version=RULES_VERSION,
        )

    rules = assess_icp_fit(name, description, tags=tags, goal=goal)
    yes_cutoff = ICP_CONFIDENT_YES if escalation_threshold is None else min(
        ICP_CONFIDENT_YES, escalation_threshold
    )

    if rules.score >= yes_cutoff or rules.score <= ICP_CONFIDENT_NO or llm is None:
        return IcpResult(
            score=float(max(0, min(100, rules.score))),
            flag=rules.score >= yes_cutoff,
            reasons=rules.reasons,
            decided_by="rules",
            classifier_version=RULES_VERSION,
        )

    verdict = classify_with_llm(name, description, goal=goal, backend=llm, model_name=model_name)
    if verdict.error:
        # LLM unavailable/erroring: fall back to the rules verdict rather than
        # silently dropping an ambiguous listing.
        return IcpResult(
            score=float(max(0, min(100, rules.score))),
            flag=rules.score >= ICP_CONFIDENT_YES,
            reasons=rules.reasons + ["llm_error"],
            decided_by="rules",
            classifier_version=RULES_VERSION,
        )

    # The verdict's own confidence gates the flag; a hedged "fit" is recorded
    # (score, reasons, decided_by="llm") but does not enter the review queue.
    confident = verdict.confidence >= min_llm_confidence
    reasons = rules.reasons + verdict.signals + ([verdict.reason] if verdict.reason else [])
    if verdict.fit and not confident:
        reasons = reasons + ["low_llm_confidence"]
    elif verdict.fit and not llm_may_flag:
        # Confident enough to be worth a look, not enough to auto-join on.
        reasons = reasons + [LLM_FIT_NEEDS_REVIEW]

    # icp_score means "how well does this fit", on every path -- rules rows
    # already store it that way, and the join queue and harvest sort by it. The
    # LLM's confidence is confidence in its *verdict*, so a confident "no" at
    # 0.9 is a fit of 10, not 90 (651 prod rows were stored the other way).
    fit_score = verdict.confidence if verdict.fit else 1.0 - verdict.confidence
    return IcpResult(
        score=round(fit_score * 100),
        flag=verdict.fit and confident and llm_may_flag,
        reasons=reasons,
        decided_by="llm",
        classifier_version=CLASSIFIER_VERSION,
    )
