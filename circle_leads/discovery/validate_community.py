"""Phase 2: existence, accessibility, and relevance checks.

Accessibility is checked with a single unauthenticated HEAD/GET against the
public landing page -- the same request any visitor's browser makes. It reads
the public page only; it does not attempt to reach content behind a login.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

import requests

from circle_leads.storage.models import AccessState

logger = logging.getLogger(__name__)

USER_AGENT = "circle-leads/0.1 (consent-first lead discovery; contact operator)"

RELEVANCE_SIGNALS: dict[str, tuple[int, list[str]]] = {
    "startup": (20, ["startup", "startups", "founder", "founders", "yc ", "seed stage"]),
    "saas": (20, ["saas", "b2b", "software business", "micro-saas"]),
    "entrepreneur": (15, ["entrepreneur", "business owner", "solopreneur", "bootstrapper"]),
    "technology": (15, ["tech", "technology", "software", "engineering", "developer", "coding", "no-code"]),
    "ai": (15, ["ai", "artificial intelligence", "machine learning", "llm", "automation"]),
    "agency": (15, ["agency", "agencies", "consultant", "freelance", "client work"]),
    "product": (10, ["product", "product manager", "mvp", "launch"]),
    "ecommerce": (10, ["ecommerce", "e-commerce", "shopify", "dtc"]),
    "marketing": (5, ["marketing", "growth", "seo"]),
}

IRRELEVANT_SIGNALS: dict[str, tuple[int, list[str]]] = {
    "hobby": (-20, ["knitting", "gardening", "cooking", "recipes", "pets", "astrology", "crochet"]),
    "fitness": (-15, ["yoga", "fitness", "weight loss", "workout", "nutrition"]),
    "personal": (-15, ["meditation", "mindfulness", "journaling", "manifestation"]),
    "fandom": (-15, ["fandom", "anime", "gaming clan", "book club"]),
}

HIRING_SPACE_HINTS = [
    "job", "jobs", "hiring", "gig", "gigs", "marketplace", "opportunit",
    "collaborat", "find a", "looking for", "talent", "recruit", "projects",
]


@dataclass
class RelevanceAssessment:
    score: int = 0
    reasons: list[str] = field(default_factory=list)

    @property
    def relevant(self) -> bool:
        return self.score >= 30


def assess_relevance(
    name: str | None, description: str | None, extra: str | None = None
) -> RelevanceAssessment:
    """Score a community's likely relevance from its public listing text."""
    haystack = " ".join(filter(None, [name, description, extra])).lower()
    if not haystack.strip():
        return RelevanceAssessment()

    assessment = RelevanceAssessment()
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

    assessment.score = max(0, min(100, assessment.score))
    return assessment


@dataclass
class AccessCheck:
    exists: bool = False
    access_status: str = AccessState.NOT_VISITED.value
    http_status: int | None = None
    name: str | None = None
    description: str | None = None
    requires_login: bool = False
    note: str | None = None


def check_public_access(
    url: str, *, timeout: int = 15, session: requests.Session | None = None
) -> AccessCheck:
    """Fetch a community's public landing page to see whether it exists.

    This is an ordinary public-page request. If the page indicates a login is
    required, the result is recorded as such -- the system does not attempt to
    authenticate against it.
    """
    http = session or requests.Session()
    try:
        resp = http.get(
            url,
            timeout=timeout,
            headers={"User-Agent": USER_AGENT},
            allow_redirects=True,
        )
    except requests.RequestException as exc:
        return AccessCheck(
            exists=False,
            access_status=AccessState.NOT_ACCESSIBLE.value,
            note=f"Request failed: {exc.__class__.__name__}",
        )

    check = AccessCheck(http_status=resp.status_code)

    if resp.status_code == 404:
        check.access_status = AccessState.NOT_ACCESSIBLE.value
        check.note = "Community not found."
        return check

    if resp.status_code >= 400:
        check.exists = resp.status_code in (401, 403)
        check.access_status = (
            AccessState.REQUIRES_MANUAL_ACTION.value
            if check.exists
            else AccessState.NOT_ACCESSIBLE.value
        )
        check.requires_login = check.exists
        check.note = f"HTTP {resp.status_code} on public landing page."
        return check

    check.exists = True
    check.access_status = AccessState.VISITED.value
    body = resp.text or ""

    title = re.search(r"<title[^>]*>(.*?)</title>", body, re.I | re.S)
    if title:
        check.name = re.sub(r"\s+", " ", title.group(1)).strip()[:200]

    meta = re.search(
        r'<meta[^>]+name=["\']description["\'][^>]+content=["\'](.*?)["\']',
        body,
        re.I | re.S,
    ) or re.search(
        r'<meta[^>]+property=["\']og:description["\'][^>]+content=["\'](.*?)["\']',
        body,
        re.I | re.S,
    )
    if meta:
        check.description = re.sub(r"\s+", " ", meta.group(1)).strip()[:1000]

    if re.search(r"\b(sign in|log in|login|join this community|request access)\b", body, re.I):
        check.requires_login = True
        check.note = "Public page indicates membership is required to view content."

    return check
