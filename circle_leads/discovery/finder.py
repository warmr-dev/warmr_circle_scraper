"""Find candidate communities from public sources and rank them for you.

The output is a shortlist you join by hand -- each row carries the join URL and
*why* it ranked, so a batch of clicks replaces an evening of hunting. This
module never joins anything and never touches your account; it reads public
listing pages and scores what it finds.

Sources, all public:
- Circle Discover (discover.circle.so), Circle's own directory.
- Any public page or search-results HTML you hand it (--from-html).
- URLs you already have (--from-file, --url).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from circle_leads.discovery.discover_communities import (
    DiscoveredCommunity,
    dedupe,
    extract_from_text,
)
from circle_leads.discovery.validate_community import assess_relevance

logger = logging.getLogger(__name__)

DISCOVER_BASE = "https://discover.circle.so"

# Discover uses /products/<slug> and /<creator> paths rather than
# <slug>.circle.so, so capture those too and keep the listing metadata.
DISCOVER_ANCHOR_RX = re.compile(
    r'<a[^>]+href=["\'](/(?:products/)?[a-z0-9][a-z0-9-]{1,80})["\'][^>]*>(.*?)</a>',
    re.I | re.S,
)

# Words in a listing that suggest members there commission software work.
BUYER_SIGNALS = {
    "founder": 25, "founders": 25, "startup": 25, "startups": 25,
    "saas": 22, "entrepreneur": 18, "entrepreneurs": 18, "business owner": 18,
    "agency": 18, "agencies": 18, "ecommerce": 15, "e-commerce": 15,
    "no-code": 15, "nocode": 15, "indie hacker": 22, "bootstrapper": 20,
    "product": 12, "tech": 12, "ai": 12, "developer": 15, "build": 10,
    "launch": 10, "mvp": 15, "freelance": 15, "hire": 20, "hiring": 25,
}

# Listings that are unlikely to contain software-hiring conversations.
NON_BUYER_SIGNALS = {
    "yoga": -30, "fitness": -25, "diet": -25, "recipe": -25, "spiritual": -25,
    "meditation": -25, "faith": -20, "bible": -25, "astrology": -30,
    "running": -20, "wellness": -20, "health coach": -25, "life coach": -20,
    "productivity": -8, "filmmaker": -15, "creator": -5,
}


@dataclass
class RankedCommunity:
    slug: str
    name: str | None
    join_url: str
    score: int
    price_label: str | None
    is_free: bool
    reasons: list[str] = field(default_factory=list)
    source: str = "discover"
    description: str | None = None

    @property
    def tier(self) -> str:
        if self.score >= 55:
            return "STRONG"
        if self.score >= 30:
            return "WORTH A LOOK"
        return "PROBABLY NOT"


def _score_listing(name: str | None, description: str | None, price: str | None) -> tuple[int, list[str]]:
    """Score a Discover listing for likely software-hiring activity."""
    haystack = " ".join(filter(None, [name, description])).lower()
    score, reasons = 0, []

    for term, weight in BUYER_SIGNALS.items():
        if re.search(rf"\b{re.escape(term)}", haystack):
            score += weight
            reasons.append(term)
    for term, weight in NON_BUYER_SIGNALS.items():
        if re.search(rf"\b{re.escape(term)}", haystack):
            score += weight
            reasons.append(f"not:{term}")

    # Reuse the community relevance model as a second opinion.
    ra = assess_relevance(name, description)
    score += ra.score // 3

    return max(0, min(100, score)), reasons


def is_free(price_label: str | None) -> bool:
    if not price_label:
        return False
    return "free" in price_label.lower()



def _clean_name(text: str) -> str:
    """Turn messy link text into a readable community name."""
    # Repair common UTF-8-as-latin1 mojibake ("Â·" -> "·").
    try:
        text = text.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        pass
    text = re.sub(r"\s+", " ", text).strip()
    # Names precede the first separator, date, or byline marker.
    text = re.split(r"\s+[—\-|:·•]\s+|\d{1,2}/\d{1,2}/\d{2,4}", text)[0].strip()
    return text[:60].strip(" -–—·•")


def rank_discover_listings(listings: list[dict]) -> list[RankedCommunity]:
    """Rank structured Discover listings.

    Each listing dict: {url_path, name, price, description?}. `url_path` is a
    Discover path like /products/<slug> or /<creator>.
    """
    ranked: list[RankedCommunity] = []
    for item in listings:
        path = item.get("url_path") or item.get("url") or ""
        if not path:
            continue
        slug = path.rstrip("/").split("/")[-1]
        name = item.get("name")
        price = item.get("price")
        desc = item.get("description")
        score, reasons = _score_listing(name, desc, price)
        ranked.append(
            RankedCommunity(
                slug=slug,
                name=name,
                join_url=DISCOVER_BASE + path if path.startswith("/") else path,
                score=score,
                price_label=price,
                is_free=is_free(price),
                reasons=reasons,
                source="discover",
                description=desc,
            )
        )
    ranked.sort(key=lambda c: (c.is_free, c.score), reverse=True)
    return ranked


def rank_extracted(communities: list[DiscoveredCommunity]) -> list[RankedCommunity]:
    """Rank plain <slug>.circle.so URLs pulled from any public text/HTML."""
    ranked: list[RankedCommunity] = []
    for c in dedupe(communities):
        score, reasons = _score_listing(c.name or c.slug, c.description, c.price_label)
        ranked.append(
            RankedCommunity(
                slug=c.slug,
                name=c.name or c.slug,
                join_url=c.url,
                score=score,
                price_label=c.price_label,
                is_free=is_free(c.price_label),
                reasons=reasons,
                source=c.source,
                description=c.description,
            )
        )
    ranked.sort(key=lambda c: c.score, reverse=True)
    return ranked


def rank_from_html(html: str, *, source: str = "html") -> list[RankedCommunity]:
    """Extract and rank communities from a saved public page's HTML.

    Handles both <slug>.circle.so links and Discover /products/<slug> paths.
    """
    communities = extract_from_text(html, source=source)
    ranked = rank_extracted(communities)

    # Also pick up Discover-style listing paths if this is a Discover page.
    seen = {r.slug for r in ranked}
    for m in DISCOVER_ANCHOR_RX.finditer(html):
        path, text = m.group(1), re.sub(r"<[^>]+>", " ", m.group(2))
        slug = path.rstrip("/").split("/")[-1]
        if slug in seen or slug in ("products", "search", "login", "signup"):
            continue
        seen.add(slug)
        # The visible link text carries the name and often a description; a
        # "Free" label in it is real signal too.
        name = _clean_name(text) or slug.replace("-", " ").title()
        score, reasons = _score_listing(text, None, text)
        ranked.append(
            RankedCommunity(
                slug=slug, name=name,
                join_url=DISCOVER_BASE + path, score=score, price_label=None,
                is_free="free" in text.lower(), reasons=reasons,
                source=f"discover:{source}",
            )
        )
    ranked.sort(key=lambda c: c.score, reverse=True)
    return ranked
