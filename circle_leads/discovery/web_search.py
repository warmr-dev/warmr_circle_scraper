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

from circle_leads.scraper.http_client import shared_session

from circle_leads.discovery.discover_communities import extract_from_text
from circle_leads.discovery.finder import RankedCommunity, rank_extracted, rank_from_html

logger = logging.getLogger(__name__)

USER_AGENT = "circle-leads/0.1 (community discovery; public pages only)"


class BackendUnavailable(Exception):
    """This backend cannot serve us at all -- move to the next one.

    Distinct from "no results" and from a transient blip. Raised only for the
    answers that will not change on a retry a minute later: a key that is
    rejected, or an account with nothing left to spend.
    """


# 401/403: the key is wrong or revoked. 402: out of credits -- which is what
# Exa returned in prod for two days while discovery quietly found nothing,
# because choose_backend picks the first *configured* backend and the failure
# was logged as the exception class name with no status attached.
DEAD_BACKEND_CODES = frozenset({401, 402, 403})

# Query templates. {q} is the user's niche, e.g. "flutter developer".
DEFAULT_QUERY_TEMPLATES = [
    "{q} community where members hire developers and freelancers",
    "{q} founders and startup community",
    "community for {q} looking for developers to build their product",
    "best online communities for {q}",
    "{q} community discussions asks and offers hiring",
    "{q} community members hiring or recruiting engineers",
    "{q} network where people post jobs and projects",
    "{q} mastermind or membership community on circle.so",
    "{q} slack alternative community for founders and builders",
    "{q} paid community for entrepreneurs and startups",
    # Fuzzy / related-topic coverage: hiring intent phrased many ways, and the
    # community *types* a hirer gathers in -- not just the literal niche term.
    "hire a {q} community",
    "need a {q} online community",
    "looking for {q} community or group",
    "{q} founders community",
    "{q} community",
    "free {q} community",
    "free online community for {q}",
    "{q} entrepreneurs community to find talent",
    "{q} indie hackers or makers community",
    "where do people who hire {q} hang out online",
]

# Keyword-engine queries that target circle.so subdomains directly. Run as a
# supplement to the primary (neural) backend, so we get both the semantic
# matches and the direct site: coverage a keyword search is good at.
SITE_QUERY_TEMPLATES = [
    'site:circle.so {q}',
    'site:circle.so {q} community',
    '"circle.so" {q} hiring OR "looking for" OR "we need"',
    '{q} "circle.so" job OR opportunity OR consultant',
    '{q} community "post a job" OR "job board" circle.so',
    '{q} circle.so members hiring OR recruiting',
    '{q} "we are looking for" OR "we need" developer circle.so',
    'inurl:circle.so {q} founders OR startup OR builders',
    # Broader net: any circle.so property, the platform's fingerprint, and
    # free-community phrasing so we catch communities that never say "hire".
    'inurl:circle.so {q}',
    '"powered by circle" {q}',
    '{q} "free community" circle.so',
    '{q} founders OR builders OR makers circle.so',
    'site:circle.so {q} "looking for" OR "need help" OR "anyone know"',
]

# Public directories worth fetching directly (no search key needed).
SEED_DIRECTORY_URLS = [
    "https://discover.circle.so/",
    "https://thehiveindex.com/communities/?platforms=circle",
    "https://thehiveindex.com/communities/?category=startups",
    "https://thehiveindex.com/communities/?category=tech",
    "https://discover.circle.so/search?q=startup",
    "https://discover.circle.so/search?q=developers",
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
        self._http = session or shared_session()
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
            _raise_if_dead("exa", resp)
            resp.raise_for_status()
        except BackendUnavailable:
            raise
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

    def find_similar(self, url: str, *, count: int = 10) -> list[SearchResult]:
        """Exa's findSimilar: pages like a given URL. Used to expand from a
        community we already have when keyword search runs dry."""
        body = {"url": url, "numResults": count}
        if self._include_domains:
            body["includeDomains"] = self._include_domains
        try:
            resp = self._http.post(
                "https://api.exa.ai/findSimilar",
                headers={"x-api-key": self._key, "Content-Type": "application/json"},
                json=body,
                timeout=25,
            )
            resp.raise_for_status()
        except requests.RequestException as exc:
            logger.warning("Exa findSimilar failed: %s", exc.__class__.__name__)
            return []
        return [
            SearchResult(title=i.get("title", "") or "", url=i.get("url", "") or "",
                         snippet=(i.get("summary") or "")[:300])
            for i in resp.json().get("results", [])
        ]


class BraveBackend:
    name = "brave"

    def __init__(self, api_key: str, session: requests.Session | None = None):
        self._key = api_key
        self._http = session or shared_session()

    def search(self, query: str, *, count: int = 10) -> list[SearchResult]:
        resp = self._http.get(
            "https://api.search.brave.com/res/v1/web/search",
            headers={"X-Subscription-Token": self._key, "Accept": "application/json"},
            params={"q": query, "count": count},
            timeout=20,
        )
        _raise_if_dead("brave", resp)
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
        self._http = session or shared_session()

    def search(self, query: str, *, count: int = 10) -> list[SearchResult]:
        resp = self._http.get(
            "https://serpapi.com/search.json",
            params={"q": query, "num": count, "api_key": self._key, "engine": "google"},
            timeout=25,
        )
        _raise_if_dead("serpapi", resp)
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
        self._http = session or shared_session()

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


def _raise_if_dead(name: str, resp: requests.Response) -> None:
    """Turn a terminal HTTP answer into BackendUnavailable, with its message."""
    if resp.status_code not in DEAD_BACKEND_CODES:
        return
    detail = (resp.text or "")[:200].replace("\n", " ")
    raise BackendUnavailable(f"{name}: HTTP {resp.status_code} {detail}")


class ChainBackend:
    """Every configured backend in order, falling through the dead ones.

    choose_backend used to return the first backend that had a key and stop
    there. When Exa's credits ran out it answered 402 to every query, the
    error was swallowed into an empty result list, and discovery found
    nothing for two days with a free keyless backend sitting right behind it.

    A backend that fails terminally is dropped for the life of the process --
    a key does not un-revoke and credits do not reappear mid-run -- and the
    first time that happens a human is told, because a search backend going
    silent is otherwise indistinguishable from the web having no answers.
    """

    name = "chain"

    def __init__(self, backends: list[SearchBackend]):
        self._backends = list(backends)
        self._dead: set[str] = set()

    @property
    def live(self) -> list[str]:
        return [getattr(b, "name", "?") for b in self._backends
                if getattr(b, "name", "?") not in self._dead]

    def search(self, query: str, *, count: int = 10) -> list[SearchResult]:
        for backend in self._backends:
            name = getattr(backend, "name", "?")
            if name in self._dead:
                continue
            try:
                results = backend.search(query, count=count)
            except BackendUnavailable as exc:
                self._dead.add(name)
                logger.error("search backend %s is out: %s", name, exc)
                _alert_backend_down(name, str(exc), self.live)
                continue
            except requests.RequestException as exc:
                # Transient: keep the backend, but let the next one answer now.
                logger.warning("search backend %s failed: %s",
                               name, exc.__class__.__name__)
                continue
            if results:
                return results
        return []

    def find_similar(self, url: str, *, count: int = 10) -> list[SearchResult]:
        for backend in self._backends:
            if getattr(backend, "name", "?") in self._dead:
                continue
            similar = getattr(backend, "find_similar", None)
            if similar is None:
                continue
            try:
                results = similar(url, count=count)
            except (BackendUnavailable, requests.RequestException):
                continue
            if results:
                return results
        return []


def _alert_backend_down(name: str, detail: str, still_live: list[str]) -> None:
    try:
        from circle_leads.notify import notify

        notify(
            f"Поиск: {name} больше не отвечает",
            f"{detail}\n\nОсталось: {', '.join(still_live) or 'ничего'}",
            level="error",
            dedup_key=f"search-backend-down:{name}",
        )
    except Exception:  # noqa: BLE001 - an alert must never break discovery
        logger.warning("could not send the search-backend alert", exc_info=True)


def choose_backend(session: requests.Session | None = None) -> SearchBackend | None:
    """Every configured backend, best first, with the keyless one last."""
    backends: list[SearchBackend] = []
    if key := os.environ.get("EXA_API_KEY"):
        backends.append(ExaBackend(key, session, include_domains=["circle.so"]))
    if key := os.environ.get("BRAVE_API_KEY"):
        backends.append(BraveBackend(key, session))
    if key := os.environ.get("SERPAPI_API_KEY"):
        backends.append(SerpApiBackend(key, session))
    # Keyless, may be rate-limited, so it is the last resort rather than absent.
    backends.append(DuckDuckGoBackend(session))
    return ChainBackend(backends)


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
    supplement_site_search: bool = True,
    expand_from: list[str] | None = None,
    expand_when_fewer_than: int = 8,
    max_results_per_query: int = 10,
    request_delay: float = 1.0,
    session: requests.Session | None = None,
) -> SearchDiscovery:
    """Search the web for Circle communities in a niche and rank them.

    Strategy:
      1. Run each niche query against the primary backend (Exa if configured).
      2. Also run keyword `site:circle.so` queries against a keyless engine as a
         supplement -- neural search finds semantic matches, site: search finds
         subdomains directly, and it's a fallback if the primary times out.
      3. Pull circle.so links out of titles + snippets; fetch listicle pages and
         extract links from them.
      4. Fetch known public directories (Discover, Hive Index) directly.
      5. Rank everything, dedup, sort.
    """
    http = session or shared_session()
    backend = backend or choose_backend(http)
    templates = templates or DEFAULT_QUERY_TEMPLATES

    result = SearchDiscovery(backend=backend.name if backend else None)
    all_html: list[str] = []
    seen_pages: set[str] = set()

    def absorb(hits) -> None:
        for hit in hits:
            all_html.append(f"{hit.title} {hit.snippet} {hit.url}")
            if fetch_result_pages and hit.url and hit.url not in seen_pages:
                seen_pages.add(hit.url)
                if _looks_like_listing(hit.url, hit.title):
                    html = _fetch(hit.url, http)
                    if html:
                        all_html.append(html)
                        result.pages_fetched += 1
                    time.sleep(request_delay)

    def run_queries(be, tmpls) -> None:
        for template in tmpls:
            query = template.format(q=niche)
            try:
                hits = be.search(query, count=max_results_per_query)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Search failed for %r: %s", query, exc.__class__.__name__)
                continue
            result.queries_run += 1
            absorb(hits)
            time.sleep(request_delay)

    # 1. Primary backend on the niche queries.
    if backend is not None:
        run_queries(backend, templates)

    # 2. Supplementary site: search on a keyword engine. If the primary is
    #    already a keyword engine, reuse it; otherwise use keyless DuckDuckGo.
    if supplement_site_search:
        if backend is not None and backend.name in ("brave", "serpapi", "duckduckgo"):
            supplement = backend
        else:
            supplement = DuckDuckGoBackend(http)
        run_queries(supplement, SITE_QUERY_TEMPLATES)
        if result.backend and supplement.name != result.backend:
            result.backend = f"{result.backend}+{supplement.name}"

    # 3. Known public directories.
    if include_directories:
        for url in SEED_DIRECTORY_URLS:
            html = _fetch(url, http)
            if html:
                all_html.append(html)
                result.pages_fetched += 1
            time.sleep(request_delay)

    # 3b. Expand from communities we already have: if the search surfaced few
    #     circle.so subdomains, ask Exa for pages similar to some known ones.
    #     This finds neighbours of your existing communities when a keyword
    #     search runs dry.
    if expand_from and hasattr(backend, "find_similar"):
        found_so_far = sum(1 for chunk in all_html if ".circle.so" in chunk)
        if found_so_far < expand_when_fewer_than:
            for seed_url in expand_from[:5]:
                try:
                    similar = backend.find_similar(seed_url, count=max_results_per_query)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("findSimilar failed: %s", exc.__class__.__name__)
                    continue
                result.queries_run += 1
                for hit in similar:
                    all_html.append(f"{hit.title} {hit.snippet} {hit.url}")
                time.sleep(request_delay)

    # 4. Rank everything gathered, keeping the best score per slug.
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
