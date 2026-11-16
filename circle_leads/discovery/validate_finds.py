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

import json
import logging
import re
from dataclasses import dataclass

import requests

from circle_leads.scraper.http_client import shared_session

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
    # For a Discover product page: where its Join button sends a visitor --
    # the community's own address (often its /checkout/... page) or an
    # outside sales page. None when the page shows no such link.
    join_link: str | None = None


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
# price at all. Only the fallback for a page without structured data
# (listing_from_html below). Next.js writes references into the page data as
# quoted "$26", "$33", ...: not amounts, hence the lookbehind -- the free
# Marketing Talent Exchange listing was stored as "$33" (2026-11-12).
_PRICE_RX = re.compile(
    r"(?<![\"\w])\$\s?([\d,]+(?:\.\d{2})?)\s*"
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


# --- a Discover product page: where it sends you, and what it costs ------------

@dataclass
class Listing:
    join_link: str | None = None
    price_label: str | None = None
    is_free: bool | None = None


_LD_JSON_RX = re.compile(
    r"<script[^>]*application/ld\+json[^>]*>(.*?)</script>", re.I | re.S)
# The listing's Join / Buy button: an anchor wrapping the "listing-cta" button.
# It points at the community's own address (a free signup page, or its
# /checkout/... for a paid tier) or at an outside sales page.
_CTA_RX = re.compile(
    r"<a\b[^>]*\bhref=\"(https?://[^\"]+)\"[^>]*>\s*<button\b[^>]*"
    r"data-testid=\"listing-cta", re.I)
# The same link in the page data, for a page whose button did not render.
_EXTERNAL_URL_RX = re.compile(r"\\?\"external_url\\?\"\s*:\s*\\?\"(https?://[^\"\\]+)")
_PERIODS = {"MON": "/month", "ANN": "/year", "WEE": "/week", "DAY": "/day"}


def _ld_offers(html: str) -> list[tuple[float, str | None]]:
    """(price, unit) for every offer in the page's schema.org Product data."""
    offers: list[tuple[float, str | None]] = []
    for match in _LD_JSON_RX.finditer(html or ""):
        try:
            data = json.loads(match.group(1))
        except ValueError:
            continue
        nodes = data.get("@graph", [data]) if isinstance(data, dict) else data
        for node in nodes if isinstance(nodes, list) else []:
            if not isinstance(node, dict) or node.get("@type") != "Product":
                continue
            items = node.get("offers") or []
            for offer in [items] if isinstance(items, dict) else items:
                if not isinstance(offer, dict):
                    continue
                spec = offer.get("priceSpecification") or {}
                try:
                    value = float(str(spec.get("price", offer.get("price"))).replace(",", ""))
                except (TypeError, ValueError):
                    continue
                offers.append((value, spec.get("unitCode")))
    return offers


def _offers_label(offers: list[tuple[float, str | None]]) -> tuple[str | None, bool | None]:
    if not offers:
        return None, None
    if any(value == 0 for value, _ in offers):
        return "Free", True   # a free tier is a way in, whatever else is sold
    value, unit = min(offers, key=lambda o: o[0])
    amount = f"{value:,.0f}" if value.is_integer() else f"{value:,.2f}"
    label = f"${amount}{_PERIODS.get(unit or '', '')}"
    if len({v for v, _ in offers}) > 1:
        label = f"From {label}"
    return label, False


def listing_from_html(html: str) -> Listing:
    """Read a Discover product page: its Join link and its price.

    The price comes from the schema.org offers the page carries, not from
    dollar signs in the HTML. The link is the listing's button, else the
    ``external_url`` in the page data. No browser is needed: both are in the
    server-rendered page (checked on 23 live listings, 2026-11-16).
    """
    import html as _html

    text = html or ""
    link = None
    cta = _CTA_RX.search(text)
    if cta:
        link = _html.unescape(cta.group(1))
    else:
        external = _EXTERNAL_URL_RX.search(text)
        if external:
            link = external.group(1).replace("\\u0026", "&")
    label, is_free = _offers_label(_ld_offers(text))
    return Listing(join_link=link, price_label=label, is_free=is_free)


def validate_url(
    url: str, *, session: requests.Session | None = None, timeout: int = 12
) -> Validation:
    """Check a community URL is real and, where possible, whether it is free."""
    http = session or shared_session()
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
        listing = listing_from_html(resp.text)
        if listing.price_label:
            price, is_free = listing.price_label, listing.is_free
        else:
            price, is_free = _extract_price(resp.text)
        return Validation(ok=True, title=_title(resp.text), is_free=is_free, price=price,
                          join_link=listing.join_link)

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
