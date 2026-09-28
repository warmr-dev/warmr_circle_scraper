"""Layer 2: LLM semantic classification for cases rules cannot settle.

The model describes a post -- who wrote it, what they ask for, what kind of
work that is -- and does not judge it. Whether the post is a lead is decided
here, by LEAD_RULE, from that description.

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

CLASSIFIER_VERSION = "ai-v2"
DEFAULT_MODEL = os.environ.get("CIRCLE_LEADS_MODEL", "claude-sonnet-5")

# --- The lead rule -----------------------------------------------------------
# Decided 2026-09-24. The client builds software ("AI apps, web apps, anything
# software"), so a post is a lead when a buyer wants software work done by
# someone else. How they want it done does not matter: an agency, a
# contractor, a hire or a technical cofounder all count. A hire for an office,
# marketing or sales seat does not, however clearly it is a hire.
#
# The model answers the four questions below; LEAD_RULE alone turns the
# answers into LEAD or NOT_LEAD. To change what counts as a lead, change the
# table -- not the prompt.
DESCRIPTION_VALUES: dict[str, tuple[str, ...]] = {
    "author_role": ("buyer", "seller", "job_seeker", "other"),
    "wants": ("service", "employee", "cofounder", "nothing"),
    "work_type": ("software", "design", "marketing", "sales", "admin",
                  "finance_legal", "content", "other"),
    "work_mode": ("remote", "onsite", "hybrid", "unknown"),
}

# LEAD if and only if every field listed here has one of its allowed values.
LEAD_RULE: dict[str, frozenset[str]] = {
    "author_role": frozenset({"buyer"}),
    "wants": frozenset({"service", "employee", "cofounder"}),
    "work_type": frozenset({"software"}),
}

# The stored reason, one sentence, written from the same answers the rule
# read -- so it can never disagree with the verdict the way a model-written
# reason could.
_WANTS_WORDS = {
    "service": "a contractor, freelancer or agency",
    "employee": "to hire someone",
    "cofounder": "a cofounder",
}
_NOT_A_LEAD_BECAUSE = {
    ("author_role", "seller"): "the author is selling their own services",
    ("author_role", "job_seeker"): "the author is looking for work",
    ("author_role", "other"):
        "nobody is buying work here (an announcement, article or discussion)",
    ("wants", "nothing"): "the post does not ask anyone to do work",
}
_WORK_WORDS = {
    "design": "design",
    "marketing": "marketing",
    "sales": "sales",
    "admin": "admin or office work",
    "finance_legal": "finance or legal work",
    "content": "content work",
    "other": "general or other work",
}

SYSTEM_PROMPT = """\
You read community posts for a lead-discovery tool that serves a software
development company. Do not decide whether a post is a lead. Describe it:
who the author is in this post, what they ask for, and what kind of work that
is. The tool decides from your answers.

author_role -- the author's role IN THIS POST:
  "buyer"       the author, their company, their client or someone they post
                for needs work done by someone else: a job opening, a gig, a
                project, an RFP, or a request to recommend a person or firm to
                do the work. Recruiters and agencies that are hiring or
                subcontracting are buyers.
  "seller"      the author offers their own services, product, agency, course
                or program: self-promotion, "we're an agency", introductions,
                portfolios, or looking for clients, customers, pilot sites or
                resellers.
  "job_seeker"  the author wants a job, gigs or projects for themselves.
  "other"       nobody is buying work: an announcement, newsletter, event,
                article, tutorial, discussion or advice question, or a search
                for tools, investors, advisors or research participants.

wants -- what the author asks for:
  "service"     work done by an agency, contractor, freelancer or consultant:
                a gig, a project, a fixed scope.
  "employee"    a person to hire into a role, full-time or part-time.
  "cofounder"   a cofounder or business partner to build the company with.
  "nothing"     no one is sought, the post says the need is already filled,
                or the author only wants advice, opinions or tool suggestions.

work_type -- the kind of work wanted. Judge the work itself, not the author's
industry and not the tools the worker would use.
  "software"      building, fixing or running software: apps, websites
                  (Shopify, Squarespace, Webflow or WordPress builds
                  included), AI and machine learning, automation,
                  integrations, data engineering, analytics and BI, cloud,
                  DevOps, IT and security engineering, or technical
                  leadership (a CTO, a technical cofounder, a technical audit).
  "design"        graphic, brand, logo, product, motion or UI mock-up design.
                  Designing, redesigning or restructuring a website is
                  "software".
  "marketing"     marketing, ads, SEO, social media, PR, lead generation,
                  influencers, community management.
  "sales"         sales, business development, account management, resellers.
  "admin"         office, administrative and executive assistants,
                  receptionists, virtual assistants, operations
                  coordination, data entry and labelling, recruiting and HR
                  (technical recruiters included), events.
  "finance_legal" accounting, bookkeeping, finance, fundraising, legal,
                  compliance paperwork.
  "content"       writing, editing, proofreading, translation, video and photo
                  production, training, teaching, speaking.
  "other"         anything else, general management such as a CEO included,
                  or nothing is wanted.
  When a post asks for several kinds of work, pick the main one.

work_mode -- "remote", "onsite", "hybrid" or "unknown", as the post states it.

Rules:
- Judge the AUTHOR's role, not vocabulary: buyers and sellers both say
  "looking for".
- Negation ("not hiring", "role filled") means wants is "nothing".
- confidence is how sure you are of author_role, wants and work_type.
- evidence_quote MUST be one continuous span copied verbatim from the post,
  showing what is wanted. Never paraphrase, and never join separate
  sentences into one quote.
- Leave a field null when the post does not state it. Never infer or invent
  budgets, company names, timelines, or contact details.

Return ONLY a JSON object:
{
  "summary": "one sentence: who asks for what",
  "author_role": "buyer" | "seller" | "job_seeker" | "other",
  "wants": "service" | "employee" | "cofounder" | "nothing",
  "work_type": "software" | "design" | "marketing" | "sales" | "admin" | "finance_legal" | "content" | "other",
  "work_mode": "remote" | "onsite" | "hybrid" | "unknown",
  "confidence": 0.0-1.0,
  "evidence_quote": "verbatim span from the post, or null",
  "job_title": null | "string",
  "skills": [],
  "employment_type": null | "Full-time"|"Part-time"|"Contract"|"Freelance"|"Unknown",
  "hire_target": null | "individual developer"|"software agency"|"freelancer"|"contractor"|"technical cofounder"|"full-time employee"|"part-time employee",
  "company": null | "string",
  "budget": null | "string",
  "location": null | "string",
  "urgency": null | "High"|"Medium"|"Low"
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
    # The model's description of the post (DESCRIPTION_VALUES keys, plus its
    # one-sentence summary). Empty when the model gave no usable answer.
    described: dict[str, str] = field(default_factory=dict)
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


class OpenAIBackend:
    """OpenAI (ChatGPT) backend. Requires OPENAI_API_KEY and the `openai` package.

    Defaults to a cheap, capable model. The classification task is small
    (one short JSON reply per ambiguous post), so a mini model is plenty.
    """

    def __init__(self, model: str | None = None, max_tokens: int = 600):
        try:
            import openai
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "Install openai to use the ChatGPT backend: pip install openai"
            ) from exc
        if not os.environ.get("OPENAI_API_KEY"):
            raise RuntimeError("OPENAI_API_KEY is not set.")
        self._client = openai.OpenAI()
        self.model = model or os.environ.get("CIRCLE_LEADS_OPENAI_MODEL", "gpt-4o-mini")
        self.max_tokens = max_tokens

    def complete(self, system: str, user: str) -> str:
        resp = self._client.chat.completions.create(
            model=self.model,
            max_tokens=self.max_tokens,
            temperature=0,  # deterministic classification
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
        return resp.choices[0].message.content or ""


class OpenRouterBackend(OpenAIBackend):
    """Any model through OpenRouter's OpenAI-compatible API.

    Requires OPENROUTER_API_KEY. The model comes from
    CIRCLE_LEADS_OPENROUTER_MODEL (an OpenRouter id such as
    ``openai/gpt-4o-mini``, the default).
    """

    def __init__(self, model: str | None = None, max_tokens: int = 600):
        try:
            import openai
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "Install openai to use the OpenRouter backend: pip install openai"
            ) from exc
        key = os.environ.get("OPENROUTER_API_KEY")
        if not key:
            raise RuntimeError("OPENROUTER_API_KEY is not set.")
        self._client = openai.OpenAI(api_key=key, base_url="https://openrouter.ai/api/v1")
        self.model = model or os.environ.get(
            "CIRCLE_LEADS_OPENROUTER_MODEL", "openai/gpt-4o-mini"
        )
        self.max_tokens = max_tokens


def make_backend() -> "LlmBackend | None":
    """Pick an available LLM backend, or None if no key is configured.

    Preference: OpenAI (OPENAI_API_KEY) then Anthropic (ANTHROPIC_API_KEY).
    Override the provider with CIRCLE_LEADS_LLM=openai|anthropic.
    """
    forced = os.environ.get("CIRCLE_LEADS_LLM", "").lower().strip()
    if forced == "openai" or (not forced and os.environ.get("OPENAI_API_KEY")):
        try:
            return OpenAIBackend()
        except RuntimeError:
            pass
    if forced == "anthropic" or os.environ.get("ANTHROPIC_API_KEY"):
        try:
            return AnthropicBackend()
        except RuntimeError:
            pass
    return None


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


def read_description(data: dict[str, Any]) -> dict[str, str] | None:
    """The model's answers to the four questions, normalised.

    None when an answer the rule reads is missing or outside its vocabulary:
    a model that did not follow the format has described nothing we can
    decide on. ``work_mode`` is only descriptive, so a bad value there
    becomes "unknown" instead.
    """
    described: dict[str, str] = {}
    for key, allowed in DESCRIPTION_VALUES.items():
        value = re.sub(r"[\s/-]+", "_", str(data.get(key) or "").strip().lower())
        if value not in allowed:
            if key in LEAD_RULE:
                return None
            value = "unknown"
        described[key] = value
    return described


def is_lead(described: dict[str, str]) -> bool:
    """The lead rule: every field in LEAD_RULE has one of its allowed values."""
    return all(described.get(key) in allowed for key, allowed in LEAD_RULE.items())


def lead_reason(described: dict[str, str], job_title: str | None = None) -> str:
    """One sentence saying why the rule filed, or did not file, the post."""
    title = f" ({str(job_title)[:80]})" if job_title else ""
    if is_lead(described):
        return (f"Lead: a buyer wants {_WANTS_WORDS[described['wants']]} "
                f"for software work{title}.")
    # Name the first field the rule rejects, in the table's order.
    for key, allowed in LEAD_RULE.items():
        value = described.get(key)
        if value in allowed:
            continue
        if key == "work_type":
            return (f"Not a lead: the work wanted is "
                    f"{_WORK_WORDS.get(value, value)}, not software{title}.")
        because = _NOT_A_LEAD_BECAUSE.get((key, value), f"{key} is {value}")
        return f"Not a lead: {because}."
    return "Not a lead."


def classify_with_llm(
    text: str, backend: LlmBackend, *, model_name: str | None = None
) -> AiVerdict:
    """Have the model describe one post, decide it by LEAD_RULE, then verify
    the model's claims against the source."""
    if not text or not text.strip():
        return AiVerdict(classification="NOT_LEAD", confidence=1.0, reason="Empty post.")

    user = f"Describe this community post:\n\n---\n{text.strip()[:6000]}\n---"
    try:
        raw = backend.complete(SYSTEM_PROMPT, user)
        data = _extract_json(raw)
    except Exception as exc:
        logger.warning("LLM classification failed: %s", exc.__class__.__name__)
        return AiVerdict(error=str(exc)[:200])

    described = read_description(data)
    if described is None:
        # No verdict rather than a guess: the caller falls back to the rules
        # and holds the post, as it does in an outage.
        logger.info("Rejected LLM reply: author_role, wants or work_type unusable")
        return AiVerdict(
            error="Model's reply lacks a usable author_role, wants or work_type.",
            model=model_name,
        )
    classification = "LEAD" if is_lead(described) else "NOT_LEAD"

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
    summary = " ".join(str(data.get("summary") or "").split())[:300]
    if summary:
        described["summary"] = summary

    return AiVerdict(
        classification=classification,
        confidence=confidence,
        reason=lead_reason(described, data.get("job_title")),
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
        described=described,
        model=model_name,
    )
