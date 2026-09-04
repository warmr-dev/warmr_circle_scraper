"""Tests for web-search community discovery, with a stub backend (no network)."""

import pytest

from circle_leads.discovery.web_search import (
    DEFAULT_QUERY_TEMPLATES,
    SITE_QUERY_TEMPLATES,
    SearchDiscovery,
    SearchResult,
    _looks_like_listing,
    _unwrap_ddg,
    discover_by_search,
)


class StubBackend:
    """Returns canned results and records the queries it was asked."""

    name = "stub"

    def __init__(self, results):
        self.results = results
        self.queries = []

    def search(self, query, *, count=10):
        self.queries.append(query)
        return self.results


def test_search_finds_and_ranks_communities():
    backend = StubBackend([
        SearchResult(
            title="SaaS Founders — the community for startup founders",
            url="https://saas-founders.circle.so",
            snippet="A community for SaaS founders building software products",
        ),
        SearchResult(
            title="Yoga Circle",
            url="https://yoga-circle.circle.so",
            snippet="Daily yoga and meditation",
        ),
    ])
    disc = discover_by_search(
        "startup founders", backend=backend,
        fetch_result_pages=False, supplement_site_search=False, include_directories=False, request_delay=0,
    )
    assert disc.backend == "stub"
    assert disc.queries_run == len(DEFAULT_QUERY_TEMPLATES)  # five query templates
    slugs = {c.slug for c in disc.ranked}
    assert "saas-founders" in slugs
    top = disc.ranked[0]
    assert top.slug == "saas-founders"
    assert top.score > 0


def test_the_niche_is_substituted_into_queries():
    backend = StubBackend([])
    discover_by_search(
        "flutter developer", backend=backend,
        fetch_result_pages=False, supplement_site_search=False, include_directories=False, request_delay=0,
    )
    assert any("flutter developer" in q for q in backend.queries)


def test_circle_links_are_pulled_from_snippets():
    backend = StubBackend([
        SearchResult(
            title="Best communities",
            url="https://example.com/blog/best",
            snippet="Check out founders-hub.circle.so and indie-devs.circle.so",
        ),
    ])
    disc = discover_by_search(
        "founders", backend=backend,
        fetch_result_pages=False, supplement_site_search=False, include_directories=False, request_delay=0,
    )
    slugs = {c.slug for c in disc.ranked}
    assert "founders-hub" in slugs
    assert "indie-devs" in slugs


def test_infrastructure_hosts_are_not_returned():
    backend = StubBackend([
        SearchResult(title="Sign in", url="https://login.circle.so/sign_in", snippet=""),
        SearchResult(title="Community", url="https://community.circle.so", snippet=""),
        SearchResult(title="Real", url="https://real-founders.circle.so", snippet="founders"),
    ])
    disc = discover_by_search(
        "founders", backend=backend,
        fetch_result_pages=False, supplement_site_search=False, include_directories=False, request_delay=0,
    )
    slugs = {c.slug for c in disc.ranked}
    assert "real-founders" in slugs
    assert "login" not in slugs
    assert "community" not in slugs


def test_empty_backend_and_no_directories_finds_nothing():
    """An empty search backend with directories off returns nothing."""
    disc = discover_by_search(
        "founders", backend=StubBackend([]),
        fetch_result_pages=False, supplement_site_search=False, include_directories=False, request_delay=0,
    )
    assert isinstance(disc, SearchDiscovery)
    assert disc.ranked == []


def test_ddg_redirect_is_unwrapped():
    wrapped = "//duckduckgo.com/l/?uddg=https%3A%2F%2Ffounders.circle.so&rut=abc"
    assert _unwrap_ddg(wrapped) == "https://founders.circle.so"


def test_unwrap_passes_through_plain_urls():
    assert _unwrap_ddg("https://x.circle.so") == "https://x.circle.so"


@pytest.mark.parametrize(
    "url,title,expected",
    [
        ("https://saas-founders.circle.so", "SaaS", True),
        ("https://example.com/best-communities", "Best founder communities", True),
        ("https://example.com/random", "Some blog post", False),
    ],
)
def test_looks_like_listing(url, title, expected):
    assert _looks_like_listing(url, title) is expected


def test_exa_backend_parses_results(monkeypatch):
    """Exa returns results[] with url + highlights; parse into SearchResult."""
    from circle_leads.discovery.web_search import ExaBackend

    class FakeResp:
        status_code = 200
        def raise_for_status(self): pass
        def json(self):
            return {"results": [
                {"url": "https://ai-founders.circle.so", "title": "AI Founders",
                 "highlights": ["a community for AI founders hiring developers"]},
            ]}

    class FakeSession:
        def post(self, *a, **k): return FakeResp()

    backend = ExaBackend("key", FakeSession())
    results = backend.search("AI founders", count=5)
    assert len(results) == 1
    assert results[0].url == "https://ai-founders.circle.so"
    assert "hiring developers" in results[0].snippet


def test_exa_is_preferred_when_key_set(monkeypatch):
    monkeypatch.setenv("EXA_API_KEY", "x")
    from circle_leads.discovery.web_search import choose_backend
    assert choose_backend().name == "exa"


# --- Supplementary site: search --------------------------------------------


def test_site_query_templates_target_circle():
    from circle_leads.discovery.web_search import SITE_QUERY_TEMPLATES
    assert any("site:circle.so" in t for t in SITE_QUERY_TEMPLATES)
    # every template mentions the niche placeholder
    assert all("{q}" in t for t in SITE_QUERY_TEMPLATES)


def test_supplement_runs_site_queries_on_keyword_engine():
    """With a keyword-engine primary, the site: queries reuse it (no extra net)."""
    class KwStub:
        name = "duckduckgo"
        def __init__(self):
            self.queries = []
        def search(self, query, *, count=10):
            self.queries.append(query)
            return []

    from circle_leads.discovery.web_search import discover_by_search
    be = KwStub()
    discover_by_search(
        "founders", backend=be,
        fetch_result_pages=False, include_directories=False,
        supplement_site_search=True, request_delay=0,
    )
    # ran the 5 niche templates + 3 site templates on the same engine
    assert any("site:circle.so" in q for q in be.queries)
    assert len(be.queries) == len(DEFAULT_QUERY_TEMPLATES) + len(SITE_QUERY_TEMPLATES)


def test_supplement_can_be_disabled():
    class KwStub:
        name = "duckduckgo"
        def __init__(self): self.queries = []
        def search(self, query, *, count=10): self.queries.append(query); return []

    from circle_leads.discovery.web_search import discover_by_search
    be = KwStub()
    discover_by_search(
        "founders", backend=be,
        fetch_result_pages=False, include_directories=False,
        supplement_site_search=False, request_delay=0,
    )
    assert not any("site:circle.so" in q for q in be.queries)
    assert len(be.queries) == len(DEFAULT_QUERY_TEMPLATES)


# --- findSimilar expansion (when search runs dry) ---------------------------


class SimilarStub:
    """Backend that returns no keyword hits but has find_similar."""
    name = "exa"
    def __init__(self):
        self.searched = []
        self.similar_calls = []
    def search(self, query, *, count=10):
        self.searched.append(query)
        return []  # keyword search comes up dry
    def find_similar(self, url, *, count=10):
        self.similar_calls.append(url)
        return [SearchResult(title="Neighbour", url="https://neighbour.circle.so", snippet="founders")]


def test_expands_from_seeds_when_search_is_dry():
    from circle_leads.discovery.web_search import discover_by_search
    be = SimilarStub()
    disc = discover_by_search(
        "obscure niche", backend=be,
        expand_from=["https://seed.circle.so"],
        fetch_result_pages=False, include_directories=False,
        supplement_site_search=False, request_delay=0,
    )
    assert be.similar_calls == ["https://seed.circle.so"]  # expansion ran
    assert any(c.slug == "neighbour" for c in disc.ranked)  # its result absorbed


def test_no_expansion_when_search_finds_enough():
    from circle_leads.discovery.web_search import discover_by_search, SearchResult as SR

    class RichStub:
        name = "exa"
        def __init__(self): self.similar_calls = []
        def search(self, query, *, count=10):
            # returns plenty of circle.so hits, so expansion shouldn't trigger
            return [SR(title="C", url=f"https://c{i}.circle.so", snippet="") for i in range(10)]
        def find_similar(self, url, *, count=10):
            self.similar_calls.append(url); return []

    be = RichStub()
    discover_by_search(
        "rich niche", backend=be, expand_from=["https://seed.circle.so"],
        expand_when_fewer_than=8, fetch_result_pages=False,
        include_directories=False, supplement_site_search=False, request_delay=0,
    )
    assert be.similar_calls == []  # enough found -> no expansion


def test_expand_from_none_is_safe():
    from circle_leads.discovery.web_search import discover_by_search
    disc = discover_by_search(
        "x", backend=SimilarStub(), expand_from=None,
        fetch_result_pages=False, include_directories=False,
        supplement_site_search=False, request_delay=0,
    )
    assert isinstance(disc.ranked, list)
