"""Broaden the set of search niches with an LLM, so discovery isn't limited to
the exact roles/skills the operator typed.

A person who wants to hire a "Flutter developer" hangs out in many places that
never say "Flutter developer" -- mobile app founders, no-code makers, startup
CTO groups, app-agency owners, adjacent tech (React Native). Searching only the
literal terms misses those communities. Given the operator's seed niches, we ask
the model for related roles, skills, tools, and community *types* a hirer would
gather in, then search those too.

This only widens SEARCH/discovery. It never touches lead classification -- the
hiring-vs-seeking judgment stays strict elsewhere.

Degrades gracefully: with no OpenAI/Anthropic key or package, or on any error,
it returns the seeds unchanged, so discovery still runs on the exact niches.
"""

from __future__ import annotations

import json
import logging
import re

logger = logging.getLogger(__name__)

_SYSTEM = (
    "You expand a lead-generation search. Given seed niches (roles/skills a "
    "consultancy wants to find HIRING demand for), list related search phrases "
    "that surface online communities where people who HIRE developers or "
    "freelancers gather. Include: adjacent job titles, the underlying skills and "
    "tools, and community TYPES (e.g. 'SaaS founders', 'no-code makers', "
    "'indie hackers', 'startup CTOs', 'app agency owners', 'mobile app "
    "founders'). Short noun phrases only, 1-4 words each. No sentences, no "
    "explanations."
)

_USER_TMPL = (
    "Seed niches:\n{seeds}\n\n"
    "Return up to {n} additional related phrases, one per line (or a JSON array "
    "of strings). Do NOT repeat the seeds."
)


def _clean(phrase: str) -> str:
    """Normalise one candidate phrase; return '' to drop it."""
    p = phrase.strip().strip("-*•\"'`").strip()
    # Drop list numbering ("1.", "2)") and stray markdown.
    p = re.sub(r"^\s*\d+[.)]\s*", "", p)
    p = re.sub(r"\s+", " ", p)
    if not p or len(p) > 60:
        return ""
    # A phrase, not a sentence.
    if len(p.split()) > 6:
        return ""
    return p


def _parse_list(text: str) -> list[str]:
    """Parse the model reply as a JSON array or a newline/comma list."""
    text = (text or "").strip()
    if not text:
        return []
    # Try JSON first (the model may wrap it in a code fence).
    fenced = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()
    for candidate in (text, fenced):
        try:
            data = json.loads(candidate)
            if isinstance(data, list):
                return [str(x) for x in data]
        except (ValueError, TypeError):
            pass
    # Fall back to newline splitting; also split a single comma-joined line.
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if len(lines) == 1 and "," in lines[0]:
        return lines[0].split(",")
    parts: list[str] = []
    for line in lines:
        parts.extend(line.split(",")) if "," in line else parts.append(line)
    return parts


def expand_niches(
    seed_niches: list[str],
    *,
    llm=None,
    max_extra: int = 20,
) -> list[str]:
    """Return the seeds plus LLM-suggested related niches (deduped, order-stable).

    ``llm`` is any object with ``complete(system, user) -> str`` (the project's
    LlmBackend). If None, we try to build one via ``make_backend()``. With no
    backend available, or on any failure, the seeds are returned unchanged.
    """
    seeds = [s.strip() for s in (seed_niches or []) if s and s.strip()]
    if not seeds or max_extra <= 0:
        return list(dict.fromkeys(seeds))  # dedupe, keep order

    if llm is None:
        try:
            from circle_leads.classifier.ai_classifier import make_backend

            llm = make_backend()
        except Exception:  # noqa: BLE001 - no key/package; stay on the seeds
            llm = None
    if llm is None:
        return list(dict.fromkeys(seeds))

    user = _USER_TMPL.format(seeds="\n".join(f"- {s}" for s in seeds), n=max_extra)
    try:
        raw = llm.complete(_SYSTEM, user)
    except Exception as exc:  # noqa: BLE001 - LLM error must not break discovery
        logger.warning("Niche expansion failed: %s", exc.__class__.__name__)
        return list(dict.fromkeys(seeds))

    seed_lower = {s.lower() for s in seeds}
    out = list(seeds)  # seeds first
    seen = set(seed_lower)
    for cand in _parse_list(raw):
        cleaned = _clean(cand)
        if not cleaned:
            continue
        key = cleaned.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(cleaned)
        if len(out) - len(seeds) >= max_extra:
            break
    return out
