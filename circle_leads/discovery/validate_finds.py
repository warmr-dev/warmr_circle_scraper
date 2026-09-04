"""Validate discovered community URLs before they reach the shortlist.

Search turns up two kinds of junk that must be filtered:

- Discover SEO/category slugs (``/startups``, ``/ai-founders-circle``) that
  return HTTP 200 but whose body says "This page could not be found." Only
  ``/products/<slug>`` Discover pages are real, and even those must be checked.
- Dead or private subdomains.

A live ``<slug>.circle.so`` community returns a real page (often a lock or
login screen) with a browser User-Agent -- a bot UA gets a 403 that looks dead
but is not. So validation uses a browser UA and inspects the body, not just the
status code.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

import requests

logger = logging.getLogger(__name__)

BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

NOT_FOUND_MARKERS = (
    "could not be found",
    "page not found",
    "doesn't exist",
    "does not exist",
)
# A subdomain that resolves but whose Circle subscription lapsed is not joinable.
DEAD_COMMUNITY_MARKERS = (
    "circle plan has expired",
    "this community is no longer active",
    "community not found",
)
FREE_MARKERS = ("free", "join for free", "$0", "no cost")
PAID_MARKERS = ("$", "/month", "/year", "per month", "subscribe", "billed")


@dataclass
class Validation:
    ok: bool
    is_free: bool | None = None
    title: str | None = None
    price: str | None = None
    reason: str = ""


# Titles served by a bot-check / interstitial page rather than the real site.
# A datacenter IP (e.g. Render) often gets these instead of the community page,
# so the extracted title is the challenge text, not the community's name. Reject
# them so the caller keeps the slug-derived name instead of storing junk.
_INTERSTITIAL_TITLE_RX = re.compile(
    r"verif\w*\s+you\s+are\s+(a\s+)?human"
    r"|just\s+a\s+moment"
    r"|attention\s+required"
    r"|checking\s+your\s+browser"
    r"|access\s+denied"
    r"|are\s+you\s+(a\s+)?human",
    re.I,
)


def is_interstitial_title(title: str | None) -> bool:
    """True if a title is a bot-check / interstitial page's text, not a real name."""
    return bool(title and _INTERSTITIAL_TITLE_RX.search(title))


def _title(html: str) -> str | None:
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
    if not m:
        return None
    import html as _html

    title = _html.unescape(re.sub(r"\s+", " ", m.group(1)).strip())[:120] or None
    if title and _INTERSTITIAL_TITLE_RX.search(title):
        return None  # bot-check page, not the real title
    return title



# A Discover product page renders the price into the HTML, e.g.
#   <h2 ...>$197</h2><span ...>/month</span>
# so pull the largest priced amount with a period, or "Free" when there is no
# price at all.
_PRICE_RX = re.compile(
    r"\$\s?([\d,]+(?:\.\d{2})?)\s*"
    r"(?:</[^>]+>\s*<[^>]+>\s*)?"
    r"(/\s?(?:mo|month|year|yr)|per\s?(?:month|year))?",
    re.I,
)


def _extract_price(html: str) -> tuple[str | None, bool | None]:
    """Return (label, is_free) from a Discover product page's HTML."""
    text = html or ""
    matches = _PRICE_RX.findall(text)
    priced = []
    for amount, period in matches:
        try:
            value = float(amount.replace(",", ""))
        except ValueError:
            continue
        # Ignore tiny fragments the SPA emits ($1, $100 with no period) unless
        # they carry a billing period.
        if value >= 1 and (period or value >= 5):
            priced.append((value, f"${amount}{(' ' + period.strip()) if period else ''}"))
    if priced:
        # The headline price is the largest amount that has a billing period,
        # else the largest amount seen.
        with_period = [p for p in priced if "/" in p[1] or "per" in p[1].lower()]
        best = max(with_period or priced, key=lambda p: p[0])
        return best[1], False
    # No price rendered anywhere -> free to join.
    if re.search(r"\bfree\b", text, re.I):
        return "Free", True
    return None, None


def validate_url(
    url: str, *, session: requests.Session | None = None, timeout: int = 12
) -> Validation:
    """Check a community URL is real and, where possible, whether it is free."""
    http = session or requests.Session()
    try:
        resp = http.get(
            url,
            headers={"User-Agent": BROWSER_UA},
            timeout=timeout,
            allow_redirects=True,
        )
    except requests.RequestException as exc:
        return Validation(ok=False, reason=f"request failed: {exc.__class__.__name__}")

    body = (resp.text or "").lower()

    if resp.status_code == 404:
        return Validation(ok=False, reason="HTTP 404")

    if "discover.circle.so" in url:
        # Discover is a client-rendered SPA whose HTML carries "404" strings
        # even on real pages, so body text can't tell real from missing. Trust
        # only /products/<slug> listings; treat bare category slugs
        # (/startups, /ai-agents) as SEO pages, not joinable communities.
        if "/products/" not in url:
            return Validation(ok=False, reason="Discover category/SEO slug, not a community")
        price, is_free = _extract_price(resp.text)
        return Validation(ok=True, title=_title(resp.text), is_free=is_free, price=price)

    if resp.status_code >= 400 and resp.status_code != 403:
        # 403 on a real community means "members only", which is fine.
        return Validation(ok=False, reason=f"HTTP {resp.status_code}")

    if any(m in body for m in DEAD_COMMUNITY_MARKERS):
        return Validation(ok=False, reason="community inactive (expired plan)")

    title = _title(resp.text)

    # Free detection is best-effort from the visible page.
    is_free: bool | None = None
    if "discover.circle.so" in url:
        has_free = any(m in body for m in FREE_MARKERS)
        has_paid = bool(re.search(r"\$\s?\d", body)) or any(
            m in body for m in ("/month", "/year", "billed")
        )
        if has_free and not has_paid:
            is_free = True
        elif has_paid:
            is_free = False

    return Validation(ok=True, is_free=is_free, title=title)


def is_subdomain_community(url: str) -> bool:
    """True for a real <slug>.circle.so community, not a Discover or infra host."""
    return bool(
        re.match(r"https?://[a-z0-9-]+\.circle\.so", url or "")
        and "discover.circle.so" not in url
        and "://login.circle.so" not in url
        and "://community.circle.so" not in url
        and "://app.circle.so" not in url
    )
