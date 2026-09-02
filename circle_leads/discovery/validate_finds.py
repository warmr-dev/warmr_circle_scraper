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
    reason: str = ""


def _title(html: str) -> str | None:
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
    if not m:
        return None
    import html as _html

    return _html.unescape(re.sub(r"\s+", " ", m.group(1)).strip())[:120] or None


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
        return Validation(ok=True, title=None)

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
