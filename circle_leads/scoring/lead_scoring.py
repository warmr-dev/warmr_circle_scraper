"""Lead scoring, 0-100, with a transparent breakdown.

Weights come from configuration so the ranking can be retuned without code
changes. Every score carries its breakdown so a reviewer can see why.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from circle_leads.classifier.extraction import titles_match
from circle_leads.classifier.lead_classifier import ClassificationResult
from circle_leads.config.settings import Requirements


def _is_recent(published_at: datetime | None, days: int) -> bool:
    if published_at is None:
        return False
    ref = published_at.replace(tzinfo=None) if published_at.tzinfo else published_at
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)
    return ref >= cutoff


def score_lead(
    result: ClassificationResult,
    requirements: Requirements,
    *,
    published_at: datetime | None = None,
) -> tuple[int, str, dict[str, Any]]:
    """Return (score, priority, breakdown)."""
    w = requirements.scoring
    extracted = result.extracted or {}
    breakdown: dict[str, Any] = {}
    total = 0

    if result.is_lead:
        total += w.hiring_intent
        breakdown["hiring_intent"] = w.hiring_intent

    title = (extracted.get("job_title") or "").lower()
    if title and any(titles_match(title, r) for r in requirements.roles_lower):
        total += w.target_role_match
        breakdown["target_role_match"] = w.target_role_match

    skills = [s.lower() for s in (extracted.get("skills") or [])]
    if any(s in requirements.skills_lower for s in skills):
        total += w.target_skill_match
        breakdown["target_skill_match"] = w.target_skill_match

    if extracted.get("budget"):
        total += w.budget_mentioned
        breakdown["budget_mentioned"] = w.budget_mentioned

    if extracted.get("company"):
        total += w.company_identified
        breakdown["company_identified"] = w.company_identified

    if _is_recent(published_at, w.recency_days):
        total += w.recent_post
        breakdown["recent_post"] = w.recent_post

    # Confidence scales the result rather than adding to it, so a shaky
    # classification cannot reach HIGH priority on signal count alone.
    if result.confidence:
        scaled = int(round(total * min(1.0, 0.6 + 0.4 * result.confidence)))
        if scaled != total:
            breakdown["confidence_scaling"] = scaled - total
        total = scaled

    total = max(0, min(100, total))
    return total, requirements.priority_for(total), breakdown
