"""Search the public web for Circle communities, then rank them.

There is no list of every <slug>.circle.so community -- Circle publishes none,
and enumerating subdomains would be a scan against their infrastructure. What
works is topic search across public sources, which is what this does: run a
handful of queries, fetch the result pages, pull out every circle.so link and
Discover listing, and rank them.

Search backend is pluggable. It picks the first that is configured:
  - Exa               (EXA_API_KEY)        -- neural search, best at finding
                                              real communities; recommended
  - Brave Search API  (BRAVE_API_KEY)      -- generous free tier
  - SerpAPI           (SERPAPI_API_KEY)
  - DuckDuckGo HTML   (no key; best-effort, may rate-limit)

Every request is the polite, public kind a browser makes. It reads listing and
result pages only; it never logs in, never joins, and never touches the user's
account.
"""

from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Callable, Protocol

import requests

from circle_leads.discovery.discover_communities import extract_from_text
from circle_leads.discovery.finder import RankedCommunity, rank_extracted, rank_from_html

logger = logging.getLogger(__name__)

USER_AGENT = "circle-leads/0.1 (community discovery; public pages only)"

# Query templates. {q} is the user's niche, e.g. "flutter developer".
DEFAULT_QUERY_TEMPLATES = [
    "{q} community where members hire developers and freelancers",
    "{q} founders and startup community",
    "community for {q} looking for developers to build their product",
    "best online communities for {q}",
    "{q} community discussions asks and offers hiring",
]

# Public directories worth fetching directly (no search key needed).
SEED_DIRECTORY_URLS = [
    "https://discover.circle.so/",
    "https://thehiveindex.com/communities/?platforms=circle",
]


@dataclass
class SearchResult:
    title: str
    url: str
    snippet: str = ""


class SearchBackend(Protocol):
    name: str

    def search(self, query: str, *, count: int = 10) -> list[SearchResult]: ...


class ExaBackend:
    """Exa neural search -- finds relevant pages semantically, not by keyword.

    Far better at surfacing real <slug>.circle.so communities than a keyword
    scrape, and it accepts a domain filter so results stay on circle.so.
    """

    name = "exa"

    def __init__(self, api_key, session=None, include_domains=None):
        self._key = api_key
        self._http = session or requests.Session()
        self._include_domains = include_domains

    def search(self, query: str, *, count: int = 10) -> list[SearchResult]:
        body = {
            "query": query,
            "type": "auto",
            "numResults": count,
            "contents": {"highlights": True},
        }
        if self._include_domains:
            body["includeDomains"] = self._include_domains
        try:
            resp = self._http.post(
                "https://api.exa.ai/search",
                headers={"x-api-key": self._key, "Content-Type": "application/json"},
                json=body,
                timeout=30,
            )
            resp.raise_for_status()
        except requests.RequestException as exc:
            logger.warning("Exa search failed: %s", exc.__class__.__name__)
            return []
        out = []
        for item in resp.json().get("results", []):
            highlights = " ".join(item.get("highlights", []) or [])
            out.append(
                SearchResult(
                    title=item.get("title", "") or "",
                    url=item.get("url", "") or "",
                    snippet=(item.get("summary") or highlights or "")[:500],
                )
            )
        return out


class BraveBackend:
    name = "brave"

    def __init__(self, api_key: str, session: requests.Session | None = None):
        self._key = api_key
        self._http = session or requests.Session()

    def search(self, query: str, *, count: int = 10) -> list[SearchResult]:
        resp = self._http.get(
            "https://api.search.brave.com/res/v1/web/search",
            headers={"X-Subscription-Token": self._key, "Accept": "application/json"},
            params={"q": query, "count": count},
            timeout=20,
        )
        resp.raise_for_status()
        out = []
        for item in resp.json().get("web", {}).get("results", []):
            out.append(
                SearchResult(
                    title=item.get("title", ""),
                    url=item.get("url", ""),
                    snippet=item.get("description", ""),
                )
            )
        return out


class SerpApiBackend:
    name = "serpapi"

    def __init__(self, api_key: str, session: requests.Session | None = None):
        self._key = api_key
        self._http = session or requests.Session()

    def search(self, query: str, *, count: int = 10) -> list[SearchResult]:
        resp = self._http.get(
            "https://serpapi.com/search.json",
            params={"q": query, "num": count, "api_key": self._key, "engine": "google"},
            timeout=25,
        )
        resp.raise_for_status()
        out = []
        for item in resp.json().get("organic_results", []):
            out.append(
                SearchResult(
                    title=item.get("title", ""),
                    url=item.get("link", ""),
                    snippet=item.get("snippet", ""),
                )
            )
        return out


class DuckDuckGoBackend:
    """Keyless fallback: scrape DuckDuckGo's HTML endpoint. Best-effort."""

    name = "duckduckgo"
    _RESULT = re.compile(
        r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>', re.I | re.S
    )
    _SNIPPET = re.compile(r'class="result__snippet"[^>]*>(.*?)</a>', re.I | re.S)

    def __init__(self, session: requests.Session | None = None):
        self._http = session or requests.Session()

    def search(self, query: str, *, count: int = 10) -> list[SearchResult]:
        try:
            resp = self._http.post(
                "https://html.duckduckgo.com/html/",
                data={"q": query},
                headers={"User-Agent": USER_AGENT},
                timeout=20,
            )
            resp.raise_for_status()
        except requests.RequestException as exc:
            logger.warning("DuckDuckGo search failed: %s", exc.__class__.__name__)
            return []

        html = resp.text
        results = []
        for href, title in self._RESULT.findall(html)[:count]:
            href = _unwrap_ddg(href)
            results.append(SearchResult(title=re.sub(r"<[^>]+>", "", title), url=href))
        return results


def _unwrap_ddg(href: str) -> str:
    """DuckDuckGo wraps outbound links in a redirect; unwrap to the real URL."""
    m = re.search(r"[?&]uddg=([^&]+)", href)
    if m:
        from urllib.parse import unquote

        return unquote(m.group(1))
    return href


def choose_backend(session: requests.Session | None = None) -> SearchBackend | None:
    """Pick the first configured backend, or None if only fetching directories."""
    if key := os.environ.get("EXA_API_KEY"):
        return ExaBackend(key, session, include_domains=["circle.so"])
    if key := os.environ.get("BRAVE_API_KEY"):
        return BraveBackend(key, session)
    if key := os.environ.get("SERPAPI_API_KEY"):
        return SerpApiBackend(key, session)
    # Keyless fallback -- may be rate-limited, so it is last.
    return DuckDuckGoBackend(session)


def _fetch(url: str, session: requests.Session, timeout: int = 20) -> str:
    try:
        resp = session.get(url, headers={"User-Agent": USER_AGENT}, timeout=timeout)
        if resp.status_code == 200 and "text/html" in resp.headers.get("content-type", ""):
            return resp.text
        return resp.text if resp.status_code == 200 else ""
    except requests.RequestException as exc:
        logger.debug("Fetch failed for %s: %s", url, exc.__class__.__name__)
        return ""


@dataclass
class SearchDiscovery:
    ranked: list[RankedCommunity] = field(default_factory=list)
    queries_run: int = 0
    pages_fetched: int = 0
    backend: str | None = None


def discover_by_search(
    niche: str,
    *,
    backend: SearchBackend | None = None,
    templates: list[str] | None = None,
    fetch_result_pages: bool = True,
    include_directories: bool = True,
    max_results_per_query: int = 10,
    request_delay: float = 1.0,
    session: requests.Session | None = None,
) -> SearchDiscovery:
    """Search the web for Circle communities in a niche and rank them.

    Strategy:
      1. Run each query template against the search backend.
      2. Pull circle.so links straight out of titles + snippets.
      3. Optionally fetch each result page and extract circle.so links from it
         (many "best communities" posts link out to the real subdomains).
      4. Fetch known public directories (Discover, Hive Index) directly.
      5. Rank everything, dedup, sort.
    """
    http = session or requests.Session()
    backend = backend or choose_backend(http)
    templates = templates or DEFAULT_QUERY_TEMPLATES

    result = SearchDiscovery(backend=backend.name if backend else None)
    all_html: list[str] = []
    seen_pages: set[str] = set()

    if backend is not None:
        for template in templates:
            query = template.format(q=niche)
            try:
                hits = backend.search(query, count=max_results_per_query)
            except Exception as exc:
                logger.warning("Search failed for %r: %s", query, exc.__class__.__name__)
                continue
            result.queries_run += 1

            for hit in hits:
                # Titles and snippets often contain the community link directly.
                all_html.append(f"{hit.title} {hit.snippet} {hit.url}")
                # Fetch the page itself if it looks like a listicle/directory.
                if fetch_result_pages and hit.url and hit.url not in seen_pages:
                    seen_pages.add(hit.url)
                    if _looks_like_listing(hit.url, hit.title):
                        html = _fetch(hit.url, http)
                        if html:
                            all_html.append(html)
                            result.pages_fetched += 1
                        time.sleep(request_delay)
            time.sleep(request_delay)

    if include_directories:
        for url in SEED_DIRECTORY_URLS:
            html = _fetch(url, http)
            if html:
                all_html.append(html)
                result.pages_fetched += 1
            time.sleep(request_delay)

    # Rank everything we gathered.
    ranked: dict[str, RankedCommunity] = {}
    for chunk in all_html:
        for rc in rank_from_html(chunk, source="web-search"):
            existing = ranked.get(rc.slug)
            if existing is None or rc.score > existing.score:
                ranked[rc.slug] = rc

    result.ranked = sorted(
        ranked.values(), key=lambda c: (c.is_free, c.score), reverse=True
    )
    return result


_LISTING_HINTS = re.compile(
    r"best|top|list|communities|directory|founders|indie|saas|circle", re.I
)


def _looks_like_listing(url: str, title: str) -> bool:
    """Only fetch pages likely to link out to real communities."""
    if "circle.so" in url:
        return True  # a community or Discover page
    return bool(_LISTING_HINTS.search(f"{url} {title}"))
