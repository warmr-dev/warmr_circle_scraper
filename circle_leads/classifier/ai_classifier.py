"""Layer 2: LLM semantic classification for cases rules cannot settle.

Two guardrails matter here:

1. ``evidence_quote`` must be an exact substring of the source text. A model
   that cannot point at real words in the post does not get to call it a lead.
2. Extracted fields are rejected when they assert facts (budget, company,
   contact details) that do not appear in the source.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Protocol

logger = logging.getLogger(__name__)

CLASSIFIER_VERSION = "ai-v1"
DEFAULT_MODEL = os.environ.get("CIRCLE_LEADS_MODEL", "claude-sonnet-5")

SYSTEM_PROMPT = """\
You classify community posts by hiring intent for a lead-discovery tool.

Decide who is being searched for and who would perform the work.

LEAD: the author (or their company) wants to ENGAGE someone else to do work.
  "We are looking for a backend developer."
  "Hiring a Flutter developer."
  "Need someone to build our mobile app."
  "Looking for an agency to rebuild our site."
  "Looking for a technical cofounder."

NOT_LEAD: the author wants work FOR THEMSELVES, or no one is being engaged.
  "I am looking for a job as a software engineer."
  "Flutter developer available for freelance projects."
  "Open to work."
  "Any companies hiring engineers?"      <- asking on their own behalf
  "We are not hiring this quarter."      <- negated
  "How do you go about hiring a dev?"    <- advice, not a request
  Vendor self-promotion, tutorials, quoted text, hypotheticals.

Rules:
- Judge the AUTHOR's role, not vocabulary. Both categories say "looking for".
- Negation ("not hiring", "role filled") makes it NOT_LEAD.
- evidence_quote MUST be copied verbatim from the post. Never paraphrase.
- Leave a field null when the post does not state it. Never infer or invent
  budgets, company names, timelines, or contact details.

Return ONLY a JSON object:
{
  "classification": "LEAD" | "NOT_LEAD",
  "confidence": 0.0-1.0,
  "reason": "one sentence",
  "evidence_quote": "verbatim span from the post, or null",
  "job_title": null | "string",
  "skills": [],
  "employment_type": null | "Full-time"|"Part-time"|"Contract"|"Freelance"|"Unknown",
  "hire_target": null | "individual developer"|"software agency"|"freelancer"|"contractor"|"technical cofounder"|"full-time employee"|"part-time employee",
  "company": null | "string",
  "budget": null | "string",
  "location": null | "string",
  "urgency": null | "High"|"Medium"|"Low",
  "disqualifiers": []
}"""


@dataclass
class AiVerdict:
    classification: str = "UNCERTAIN"
    confidence: float = 0.0
    reason: str = ""
    evidence_quote: str | None = None
    job_title: str | None = None
    skills: list[str] = field(default_factory=list)
    employment_type: str | None = None
    hire_target: str | None = None
    company: str | None = None
    budget: str | None = None
    location: str | None = None
    urgency: str | None = None
    disqualifiers: list[str] = field(default_factory=list)
    model: str | None = None
    error: str | None = None


class LlmBackend(Protocol):
    """Any callable that turns a prompt into raw model text."""

    def complete(self, system: str, user: str) -> str: ...


class AnthropicBackend:
    """Claude backend. Requires ANTHROPIC_API_KEY and the `anthropic` package."""

    def __init__(self, model: str = DEFAULT_MODEL, max_tokens: int = 1024):
        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "Install the LLM extra to enable semantic classification: "
                "pip install 'circle-leads[llm]'"
            ) from exc
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise RuntimeError("ANTHROPIC_API_KEY is not set.")
        self._client = anthropic.Anthropic()
        self.model = model
        self.max_tokens = max_tokens

    def complete(self, system: str, user: str) -> str:
        resp = self._client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        return "".join(
            block.text for block in resp.content if getattr(block, "type", "") == "text"
        )


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip().lower()


def verify_evidence(quote: str | None, source: str) -> bool:
    """The quote must actually appear in the post, modulo whitespace."""
    if not quote:
        return False
    return _normalize(quote) in _normalize(source)


def _extract_json(raw: str) -> dict[str, Any]:
    raw = raw.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.S)
    if fence:
        raw = fence.group(1)
    else:
        start, end = raw.find("{"), raw.rfind("}")
        if start != -1 and end > start:
            raw = raw[start : end + 1]
    return json.loads(raw)


def _drop_unsupported(value: str | None, source: str) -> str | None:
    """Discard an extracted field whose content is not present in the source."""
    if not value or str(value).strip().lower() in ("unknown", "n/a", "none", "null"):
        return None
    return value if _normalize(str(value)) in _normalize(source) else None


def classify_with_llm(
    text: str, backend: LlmBackend, *, model_name: str | None = None
) -> AiVerdict:
    """Classify one post, then verify the model's claims against the source."""
    if not text or not text.strip():
        return AiVerdict(classification="NOT_LEAD", confidence=1.0, reason="Empty post.")

    user = f"Classify this community post:\n\n---\n{text.strip()[:6000]}\n---"
    try:
        raw = backend.complete(SYSTEM_PROMPT, user)
        data = _extract_json(raw)
    except Exception as exc:
        logger.warning("LLM classification failed: %s", exc.__class__.__name__)
        return AiVerdict(error=str(exc)[:200])

    classification = str(data.get("classification", "UNCERTAIN")).upper()
    if classification not in ("LEAD", "NOT_LEAD"):
        classification = "UNCERTAIN"

    try:
        confidence = max(0.0, min(1.0, float(data.get("confidence", 0.0))))
    except (TypeError, ValueError):
        confidence = 0.0

    quote = data.get("evidence_quote")
    if classification == "LEAD" and not verify_evidence(quote, text):
        # A LEAD the model cannot ground in real words is downgraded, not
        # trusted. This is the guard against confident hallucination.
        logger.info("Rejected LEAD verdict: evidence quote not found in source")
        return AiVerdict(
            classification="UNCERTAIN",
            confidence=0.0,
            reason="Model's evidence quote was not present in the source text.",
            model=model_name,
            disqualifiers=["unverified_evidence"],
        )

    skills = data.get("skills") or []
    if not isinstance(skills, list):
        skills = []

    return AiVerdict(
        classification=classification,
        confidence=confidence,
        reason=str(data.get("reason", ""))[:500],
        evidence_quote=quote if verify_evidence(quote, text) else None,
        job_title=data.get("job_title"),
        skills=[str(s) for s in skills][:20],
        employment_type=data.get("employment_type"),
        hire_target=data.get("hire_target"),
        # Facts that must be grounded, since inventing them misleads a reviewer.
        company=_drop_unsupported(data.get("company"), text),
        budget=_drop_unsupported(data.get("budget"), text),
        location=data.get("location"),
        urgency=data.get("urgency"),
        disqualifiers=[str(d) for d in (data.get("disqualifiers") or [])],
        model=model_name,
    )
