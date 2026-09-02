"""Layer 3: extract hiring details from text using deterministic patterns.

Used on its own when no LLM is configured, and as a fallback for fields the
model left null.
"""

from __future__ import annotations

import re
from typing import Any

from circle_leads.classifier.keyword_rules import BUDGET_PATTERN

EMPLOYMENT_PATTERNS = [
    ("Full-time", r"\bfull[\s-]?time\b|\bfte\b|\bpermanent\b|\bsalaried\b"),
    ("Part-time", r"\bpart[\s-]?time\b"),
    ("Contract", r"\bcontract(?:or)?\b|\bcontract[\s-]?to[\s-]?hire\b|\bb2b\b"),
    ("Freelance", r"\bfreelance(?:r)?\b|\bgig\b|\bper[\s-]?project\b"),
    ("Internship", r"\bintern(?:ship)?\b"),
]

HIRE_TARGET_PATTERNS = [
    ("technical cofounder", r"\b(?:technical\s+)?co-?founder\b|\bcto\s+(?:cofounder|partner)\b"),
    ("software agency", r"\b(?:agency|agencies|studio|dev\s*shop|consultancy|firm)\b"),
    ("freelancer", r"\bfreelance(?:r)?\b"),
    ("contractor", r"\bcontractor\b|\bcontract\s+(?:developer|engineer|work)\b"),
    ("part-time employee", r"\bpart[\s-]?time\s+(?:developer|engineer|hire|role)\b"),
    ("full-time employee", r"\bfull[\s-]?time\s+(?:developer|engineer|hire|role|employee)\b"),
    ("individual developer", r"\b(?:developer|engineer|programmer|dev)\b"),
]

URGENCY_PATTERNS = [
    ("High", r"\basap\b|\burgent(?:ly)?\b|\bimmediately\b|\bright\s+away\b|\bthis\s+week\b|\bstart\s+(?:now|monday|today)\b"),
    ("Medium", r"\bnext\s+month\b|\bwithin\s+\d+\s+weeks?\b|\bby\s+(?:end\s+of\s+)?(?:the\s+)?(?:month|quarter)\b|\bsoon\b"),
    ("Low", r"\bno\s+rush\b|\bflexible\s+(?:timeline|start)\b|\bexploring\b|\bplanning\s+(?:ahead|for)\b"),
]

LOCATION_PATTERNS = [
    ("Remote", r"\b(?:100%\s+)?remote\b|\bwork\s+from\s+(?:home|anywhere)\b|\bdistributed\s+team\b"),
    ("Hybrid", r"\bhybrid\b"),
    ("On-site", r"\bon[\s-]?site\b|\bin[\s-]?office\b|\bin[\s-]?person\b"),
]

# Each optional qualifier owns its own trailing space, so no two quantifiers
# compete for the same whitespace run. The earlier `...?\s*...?\s*` chain was
# quadratic on long runs.
_ROLE_RX = re.compile(
    r"\b((?:(?:senior|junior|mid[\s-]?level|lead|principal|staff|entry[\s-]?level)\s+)?"
    r"(?:(?:back[\s-]?end|front[\s-]?end|full[\s-]?stack|mobile|web|cloud|data|ml|ai|devops|qa|site\s+reliability)\s+)?"
    r"(?:developer|engineer|programmer|architect|designer|scientist))\b",
    re.I,
)
_NAMED_ROLE_RX = re.compile(
    r"\b((?:flutter|react(?:\s+native)?|node(?:\.?js)?|python|java|golang|go|rust|ruby|php|"
    r"swift|kotlin|android|ios|django|rails|laravel|vue|angular|typescript|\.net|c\+\+|c#)"
    r"\s+(?:developer|engineer|dev))\b",
    re.I,
)
_BUDGET_RX = re.compile(BUDGET_PATTERN, re.I)
_COMPANY_RX = re.compile(
    r"\b(?:at|for|join)\s+([A-Z][\w&.-]*(?:\s+[A-Z][\w&.-]*){0,3})"
    r"(?=[,.]|\s+(?:we|is|are|has|the|and)\b)"
)


# Bare role nouns match every configured role as a substring ("developer" is
# inside "flutter developer"), so a generic extracted title must not satisfy a
# specific requirement. These nouns are also used interchangeably in practice:
# a post saying "Backend Engineer" answers a "Backend Developer" requirement.
GENERIC_ROLE_NOUNS = {
    "developer", "engineer", "programmer", "dev", "coder",
    "designer", "architect", "scientist",
}
INTERCHANGEABLE_ROLE_NOUNS = {"developer", "engineer", "programmer", "dev", "coder"}


def titles_match(extracted: str, configured: str) -> bool:
    """True when an extracted job title satisfies a configured target role."""
    extracted = " ".join((extracted or "").lower().split())
    configured = " ".join((configured or "").lower().split())
    if not extracted or not configured:
        return False
    if extracted == configured:
        return True
    if extracted in GENERIC_ROLE_NOUNS:
        # A bare "developer" does not satisfy a specific role requirement.
        return False

    e_words, c_words = set(extracted.split()), set(configured.split())
    e_qualifiers = e_words - GENERIC_ROLE_NOUNS
    c_qualifiers = c_words - GENERIC_ROLE_NOUNS
    if not c_qualifiers:
        return bool(e_words & c_words)

    # The qualifiers must overlap ("backend" == "backend"), and the role nouns
    # must be compatible ("engineer" ~ "developer").
    if not (e_qualifiers & c_qualifiers):
        return False
    e_nouns = e_words & GENERIC_ROLE_NOUNS
    c_nouns = c_words & GENERIC_ROLE_NOUNS
    if not e_nouns or not c_nouns:
        return True
    if e_nouns & c_nouns:
        return True
    return bool(
        e_nouns <= INTERCHANGEABLE_ROLE_NOUNS and c_nouns <= INTERCHANGEABLE_ROLE_NOUNS
    )


def extract_job_title(text: str) -> str | None:
    """Prefer a technology-qualified title ("Flutter Developer") when present."""
    # Normalize here too rather than relying on the caller having done it.
    text = re.sub(r"\s+", " ", text or "")
    named = _NAMED_ROLE_RX.search(text)
    if named:
        return " ".join(named.group(1).split()).title()
    generic = _ROLE_RX.search(text)
    if generic:
        title = " ".join(generic.group(1).split()).strip()
        return title.title() if title else None
    return None


def extract_skills(text: str, target_skills: list[str]) -> list[str]:
    """Match only against the configured skill vocabulary."""
    found = []
    for skill in target_skills:
        pattern = re.escape(skill).replace(r"\ ", r"\s+")
        if re.search(rf"(?<![\w.]){pattern}(?![\w])", text, re.I):
            found.append(skill)
    return found


def _first_match(text: str, patterns: list[tuple[str, str]]) -> str | None:
    for label, rx in patterns:
        if re.search(rx, text, re.I):
            return label
    return None


def extract_employment_type(text: str) -> str | None:
    return _first_match(text, EMPLOYMENT_PATTERNS)


def extract_hire_target(text: str) -> str | None:
    return _first_match(text, HIRE_TARGET_PATTERNS)


def extract_urgency(text: str) -> str | None:
    return _first_match(text, URGENCY_PATTERNS)


def extract_location(text: str) -> str | None:
    return _first_match(text, LOCATION_PATTERNS)


def extract_budget(text: str) -> str | None:
    m = _BUDGET_RX.search(text or "")
    return m.group(0).strip() if m else None


def extract_company(text: str) -> str | None:
    m = _COMPANY_RX.search(text or "")
    if not m:
        return None
    candidate = m.group(1).strip()
    # Filter sentence-initial capitalization that is not a company name.
    if candidate.lower() in {"i", "we", "the", "our", "you", "remote", "hi", "hello"}:
        return None
    return candidate if 1 < len(candidate) < 80 else None


def extract_all(text: str, target_skills: list[str]) -> dict[str, Any]:
    return {
        "job_title": extract_job_title(text),
        "skills": extract_skills(text, target_skills),
        "employment_type": extract_employment_type(text),
        "hire_target": extract_hire_target(text),
        "company": extract_company(text),
        "budget": extract_budget(text),
        "location": extract_location(text),
        "urgency": extract_urgency(text),
    }
