"""Semantic classification of current/future commercial buyer demand.

The model describes a post -- who wrote it, what they ask for, what kind of
work that is -- and does not judge it. Whether the post is a lead is decided
here, by LEAD_RULE, from that description.

Two guardrails matter here:

1. ``evidence_quote`` must be an exact substring of the source text. A model
   that cannot ground either decision produces a retryable processing error.
2. Extracted fields are rejected when they assert facts (budget, company,
   contact details) that do not appear in the source.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
from dataclasses import dataclass, field
from typing import Any, Protocol

logger = logging.getLogger(__name__)

CLASSIFIER_VERSION = "ai-v3"
POLICY_VERSION = "commercial-demand-v1"
SCOUT_POLICY_REF = "c4d9dfffa193aea05e1929668dec34946f79f5d5"
MAX_CONTEXT_CHARS = 100000
DEFAULT_MODEL = os.environ.get("CIRCLE_LEADS_MODEL", "claude-sonnet-5")

# Descriptive categories are routing metadata, never an admission whitelist.
DESCRIPTION_VALUES: dict[str, tuple[str, ...]] = {
    "author_role": ("buyer", "seller", "job_seeker", "other"),
    "service_direction": ("seeking_help", "offering_help", "neither", "mixed"),
    "post_purpose": ("demand", "mixed_demand", "information", "supplier_offer",
                     "social", "career_advice", "generic_inhouse_hiring", "closed_need"),
    "wants": ("service", "employee", "cofounder", "nothing"),
    "work_type": ("software", "design", "marketing", "sales", "admin",
                  "finance_legal", "content", "other"),
    "work_mode": ("remote", "onsite", "hybrid", "unknown"),
    "demand_signal": ("explicit_demand", "recommendation_request", "buying_research",
                      "switching_intent", "problem_intent", "solution_exploration",
                      "hiring_signal", "none"),
}
LEAD_RULE = {
    "author_role": frozenset({"buyer"}),
    "post_purpose": frozenset({"demand", "mixed_demand"}),
    "service_direction": frozenset({"seeking_help", "mixed"}),
    "demand_signal": frozenset(set(DESCRIPTION_VALUES["demand_signal"]) - {"none"}),
}

SYSTEM_PROMPT = """First identify the direction of service/work: does the CURRENT AUTHOR want
help delivered TO them/their company, or offer to deliver help TO other people?
An offer of a free or paid audit is supplier promotion, not that supplier's
buyer demand. Asking prospects about their pain points is supplier discovery,
not buying help. Hiring an employee/contractor means seeking work delivered TO
the company and can be demand even when the company is also a service provider.
Then describe the CURRENT POST's purpose. Is the author asking for help,
reporting an unresolved business problem, or sharing information/a completed
solution? A completed tutorial is information, not exploration of an unmet need.
Then classify CURRENT OR FUTURE BUYER DEMAND across ANY commercial category.
Evaluate the complete CURRENT POST and the supplied root/replies as context.
Only classify demand attributable to the CURRENT POST's author or the buyer
explicitly represented by them. Context is not a new lead: a supplier reply,
recommendation offered by another person, or social acknowledgement in a buyer
thread is NOT buyer demand by that replying author.

Include requests for vendors, agencies, consultants, contractors and freelancers;
provider recommendations/comparisons/switching; concrete commercial problems
and solution exploration; specialist or leadership hiring with a concrete skill
that can create recruiting, staffing or service demand. Marketing, SEO, creative,
product/UX consulting, Webflow, webinars, videography, recruiting and every other
commercial category qualify. The category is metadata, never a whitelist.
Missing budget, missing timeline, no matching vendor, freelancer/part-time format
and early-stage readiness are NEVER grounds for rejection.

Exclude informational content without buyer demand, tutorials, supplier
self-promotion, showcases, generic social discussion, career/job-search advice,
and generic in-house hiring without a concrete commercial-service signal.
A buyer describing their own agency before requesting help is still a buyer.
Generic networking, an introduction with "would love to connect / have questions",
requests to participate in the author's poll/survey, and personal career/leadership
transition advice are not commercial demand without an actual provider request
or a concrete unresolved business problem. Do not invent that missing problem.
A provider learning about customer needs or asking people to choose a portfolio
photo is not requesting commercial help. Conversely, a specific current business
project/problem can qualify as solution exploration without a vendor request.
Read negation in context: "not hiring staff, need an SEO agency" is demand.
"Role filled" with no remaining need is not. Keywords alone do not decide.
Start with the CURRENT AUTHOR's intent: their request, unresolved commercial
problem, or solution research. A tutorial, tips, or a demonstration of a solution
they already built is INFORMATION, even when it mentions lead generation,
vendors, calculators, costs or paid tools. Such an author is not a buyer merely
because replies contain other people's questions or commercial needs.
A tutorial describing building tools is not a request to buy work.
Do not speculate that an author "may seek services", "might need assistance",
"hints at a need" or "could benefit" just because they describe a commercial
activity. There must be an actual request, unresolved concrete commercial
problem, or future need expressed by the current author. Reporting a successful
method is not an unresolved problem. Other commenters' questions are not this
author's demand.

Examples (apply the intent, not these keywords):
- "I'm joining a new industry. Who else works here? Let's connect, I have
  questions." -> neither / social / other / none. Unspecified questions and
  peer networking do not express a concrete business problem.
- "Help my research: complete this survey about your marketing challenges."
  -> neither / information / other / none. Asking readers to contribute to
  the author's research does not mean the author wants to buy help.
- "I offer consulting and want to meet companies who need my skills."
  -> offering_help / supplier_offer / seller / none. The author supplies work.
- "I'm becoming a manager. Any advice on thinking like a leader?"
  -> neither / career_advice / other / none. A vague future intention to fill
  team gaps does not turn personal leadership advice into specialist hiring.
- "I installed an assistant. Here are tasks I gave it and results it achieved."
  -> neither / information / other / none. This reports use of a solution,
  rather than seeking another solution or describing an unresolved limitation.
- "What networking meetups are people attending?" -> neither / social /
  other / none. Contrast "Need a venue/caterer for our customer event", which
  is a concrete commercial provider request.
- "Here is how I built our reporting dashboard. Follow these steps; hope it
  helps." -> other / none. This shares a completed solution, no buyer demand.
- "Our reporting is unreliable and causes missed renewals. What options should
  we explore?" -> buyer / solution_exploration. An unresolved business problem.
- "We are hiring an experienced operations leader." -> buyer / hiring_signal.
  Evidence: "hiring an experienced operations leader" (one short exact span).
- "We make dashboards; contact me to buy one." -> seller / none.
A company/product introduction alone is not demand. An actual paid freelance
marketing role is demand, even when preceded by a long company introduction.

Return ONLY a JSON object with these fields:
- service_direction: "seeking_help" | "offering_help" | "neither" | "mixed"
  seeking_help means work, advice about an unresolved business problem, a
  solution, vendor or specialist is sought FOR the current author/company.
  offering_help means the current author offers services/audits to readers.
  neither covers networking, tips, surveys and personal career discussion.
  mixed requires an actual inbound need in this current post, not in replies.
- post_purpose: "demand" | "mixed_demand" | "information" | "supplier_offer" |
  "social" | "career_advice" | "generic_inhouse_hiring" | "closed_need"
  Use mixed_demand only if the CURRENT AUTHOR also expresses a real request or
  unresolved commercial problem alongside other content. Tips, tutorials and
  successful completed solutions with no remaining need are information.
- author_role: "buyer" | "seller" | "job_seeker" | "other"
- wants: "service" | "employee" | "cofounder" | "nothing"
  A problem or research ask can have wants="nothing" and still be demand.
- work_type: "software" | "design" | "marketing" | "sales" | "admin" |
  "finance_legal" | "content" | "other"
- work_mode: "remote" | "onsite" | "hybrid" | "unknown"
- demand_signal: "explicit_demand" | "recommendation_request" | "buying_research" |
  "switching_intent" | "problem_intent" | "solution_exploration" | "hiring_signal" | "none"
- awareness: integer 1..5 for demand, null otherwise (2=problem, 3=exploring,
  4=comparing/recommendations, 5=explicit ask; 1=latent commercial need)
- confidence: number 0..1
- summary: one sentence (REQUIRED)
- reason: one sentence explaining why the current author has demand or why
  this is excluded (REQUIRED for BOTH demand and non-demand)
- supporting_excerpts: nonempty list of {"source_id": "current" or supplied
  context source_id, "quote": "one continuous verbatim span"}. Supply evidence
  for BOTH positive and negative decisions. Use ONE short current-post span.
  Add a context span only if essential for disambiguation; do not add a second
  paraphrased current span. Copy a SHORT EXACT span (5-12 words is enough).
  Never paraphrase, insert ellipses, join separated spans or abbreviate job titles.
  If two separate spans support the decision, use two separate excerpt objects.
- evidence_quote: one verbatim current-post span (compatibility field)
- job_title, employment_type, hire_target, company, budget, location, urgency:
  string or null; skills: list of strings.
Before returning, check every quote by literal copying from the source.
Do not summarize a list of roles inside a quote: copy only a short part of one
sentence. NEVER put "..." in a quote unless those literal dots are in the source.
All listed JSON keys are required. Never omit summary, reason or excerpts.
Use this complete shape, replacing values with the actual assessment:
{"service_direction":"neither","post_purpose":"information","author_role":"other","wants":"nothing","work_type":"other",
 "work_mode":"unknown","demand_signal":"none","awareness":null,
 "confidence":0.9,"summary":"Describe the current post.",
 "reason":"Explain current author intent and the inclusion/exclusion.",
 "supporting_excerpts":[{"source_id":"current","quote":"COPY EXACT SOURCE SPAN"}],
 "evidence_quote":"COPY EXACT CURRENT SPAN","job_title":null,"skills":[],
 "employment_type":null,"hire_target":null,"company":null,"budget":null,
 "location":null,"urgency":null}
Do not invent facts, budgets, timestamps or contact information. Unstated facts
are null. Describe the current author's intent, not vocabulary in other replies.
"""


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
    demand_signal: str = "none"
    awareness: int | None = None
    supporting_excerpts: list[dict] = field(default_factory=list)


class LlmBackend(Protocol):
    """Any callable that turns a prompt into raw model text."""

    def complete(self, system: str, user: str) -> str: ...


class AnthropicBackend:
    """Claude backend. Requires ANTHROPIC_API_KEY and the `anthropic` package."""

    def __init__(self, model: str = DEFAULT_MODEL, max_tokens: int = 2000):
        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "Install the LLM extra to enable semantic classification: "
                "pip install 'circle-leads[llm]'"
            ) from exc
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise RuntimeError("ANTHROPIC_API_KEY is not set.")
        self._client = anthropic.Anthropic(timeout=45, max_retries=0)
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

    def __init__(self, model: str | None = None, max_tokens: int = 2000):
        try:
            import openai
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "Install openai to use the ChatGPT backend: pip install openai"
            ) from exc
        if not os.environ.get("OPENAI_API_KEY"):
            raise RuntimeError("OPENAI_API_KEY is not set.")
        self._client = openai.OpenAI(timeout=45, max_retries=0)
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

    def __init__(self, model: str | None = None, max_tokens: int = 2000):
        try:
            import openai
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "Install openai to use the OpenRouter backend: pip install openai"
            ) from exc
        key = os.environ.get("OPENROUTER_API_KEY")
        if not key:
            raise RuntimeError("OPENROUTER_API_KEY is not set.")
        self._client = openai.OpenAI(api_key=key, base_url="https://openrouter.ai/api/v1", timeout=45, max_retries=0)
        self.model = model or os.environ.get(
            "CIRCLE_LEADS_OPENROUTER_MODEL", "openai/gpt-4o-mini"
        )
        self.max_tokens = max_tokens


def make_backend() -> "LlmBackend | None":
    """Pick an available LLM backend, or None if no key is configured.

    Preference: OpenAI, then Anthropic, then OpenRouter.
    Override the provider with CIRCLE_LEADS_LLM=openai|anthropic|openrouter.
    """
    forced = os.environ.get("CIRCLE_LEADS_LLM", "").lower().strip()
    if forced == "openrouter":
        try:
            return OpenRouterBackend()
        except RuntimeError:
            return None
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
    if not forced and os.environ.get("OPENROUTER_API_KEY"):
        try:
            return OpenRouterBackend()
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
    """The model's descriptive fields, normalised.

    None when an answer the rule reads is missing or outside its vocabulary:
    a model that did not follow the format has described nothing we can
    decide on. ``work_mode`` is only descriptive, so a bad value there
    becomes "unknown" instead.
    """
    described: dict[str, str] = {}
    for key, allowed in DESCRIPTION_VALUES.items():
        value = re.sub(r"[\s/-]+", "_", str(data.get(key) or "").strip().lower())
        if value not in allowed:
            if key != "work_mode":
                return None
            value = "unknown"
        described[key] = value
    return described


def is_lead(described: dict[str, str]) -> bool:
    """The lead rule: every field in LEAD_RULE has one of its allowed values."""
    return all(described.get(key) in allowed for key, allowed in LEAD_RULE.items())


def lead_reason(described: dict[str, str], job_title: str | None = None) -> str:
    if is_lead(described):
        return f"Lead: current or future commercial demand ({described['demand_signal']})."
    return f"Not a lead: author_role={described.get('author_role')}, post_purpose={described.get('post_purpose')}, service_direction={described.get('service_direction')}, demand_signal={described.get('demand_signal')}."


def classify_with_llm(
    text: str, backend: LlmBackend, *, model_name: str | None = None,
    context: list[dict] | None = None, current_metadata: dict | None = None
) -> AiVerdict:
    """Have the model describe one post, decide it by LEAD_RULE, then verify
    the model's claims against the source."""
    if not text or not text.strip():
        return AiVerdict(classification="NOT_LEAD", confidence=1.0, reason="Empty post.")

    sources = {"current": text}
    for item in context or []:
        source_id = str(item["source_id"])
        if source_id in sources:
            return AiVerdict(error="duplicate_context_source_id", model=model_name)
        sources[source_id] = item["content"]
    user = json.dumps({"current_post": {**(current_metadata or {}), "source_id": "current", "content": text},
                       "context": context or []}, ensure_ascii=False)
    if len(user) > MAX_CONTEXT_CHARS:
        return AiVerdict(error="context_limit_exceeded", model=model_name)

    try:
        raw = backend.complete(SYSTEM_PROMPT, user)
        data = _extract_json(raw)
        if not isinstance(data, dict):
            raise ValueError("Model reply must be a JSON object")
    except Exception as exc:
        logger.warning("LLM classification failed: %s", exc.__class__.__name__)
        return AiVerdict(error=str(exc)[:200], model=model_name or getattr(backend, "model", None))

    described = read_description(data)
    if described is None:
        # Unusable descriptions are processing failures, never negative labels.
        logger.info("Rejected LLM reply: author_role, wants or work_type unusable")
        return AiVerdict(
            error="Model reply lacks a usable commercial-demand description.",
            model=model_name,
        )
    classification = "LEAD" if is_lead(described) else "NOT_LEAD"

    try:
        raw_confidence = float(data["confidence"])
        if not math.isfinite(raw_confidence):
            raise ValueError("non-finite confidence")
        confidence = max(0.0, min(1.0, raw_confidence))
    except (KeyError, TypeError, ValueError):
        return AiVerdict(error="Invalid or missing confidence.", model=model_name)

    excerpts = data.get("supporting_excerpts")
    reason = data.get("reason")
    aware = data.get("awareness")
    if (not isinstance(excerpts, list) or not excerpts or not isinstance(reason, str)
            or not reason.strip() or (classification == "LEAD" and
            (type(aware) is not int or aware not in range(1, 6)))):
        return AiVerdict(error="Model reply lacks reason, supporting excerpts or awareness.",
                         model=model_name)
    for key in ("job_title", "employment_type", "hire_target", "company", "budget", "location", "urgency"):
        if data.get(key) is not None and not isinstance(data[key], str):
            return AiVerdict(error=f"Invalid extracted field: {key}", model=model_name)
    for excerpt in excerpts:
        if (not isinstance(excerpt, dict) or
                not isinstance(excerpt.get("quote"), str) or
                not isinstance(excerpt.get("source_id"), str) or
                not verify_evidence(excerpt.get("quote"), sources.get(excerpt.get("source_id"), ""))):
            return AiVerdict(error="unverified_evidence", model=model_name,
                             disqualifiers=["unverified_evidence"])
    current = [x["quote"] for x in excerpts if x.get("source_id") == "current"]
    if not current:
        return AiVerdict(error="Evidence must include the current post.", model=model_name)
    quote = current[0]

    skills = data.get("skills") or []
    if not isinstance(skills, list):
        skills = []
    summary = " ".join(str(data.get("summary") or "").split())[:300]
    if summary:
        described["summary"] = summary

    return AiVerdict(
        classification=classification,
        confidence=confidence,
        reason=reason.strip() if classification == "LEAD" else lead_reason(described) + " " + reason.strip(),
        demand_signal=described["demand_signal"],
        awareness=aware if classification == "LEAD" else None,
        supporting_excerpts=excerpts,
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
