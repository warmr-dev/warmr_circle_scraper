"""Circle directory crawl: pagination/dedup logic and slug derivation, stubbed
against a fake Playwright `page` (no real browser/network)."""

import tempfile

from sqlalchemy import select

from circle_leads.discovery import circle_directory
from circle_leads.discovery.circle_directory import (
    DirectoryGoal,
    DirectoryListing,
    _to_ranked,
    crawl_listings,
    fetch_goals,
    recheck_unresolved_join_urls,
)
from circle_leads.discovery.discover_communities import PLATFORM_DISCOVER
from circle_leads.discovery.join_type import JoinClassification, JoinType
from circle_leads.storage.database import Database, get_or_create_community
from circle_leads.storage.models import Community


class StubResponse:
    def __init__(self, status, payload):
        self.status = status
        self._payload = payload

    def json(self):
        return self._payload


class StubPage:
    """Fakes page.request.get(url) -> StubResponse, routed by URL substring."""

    def __init__(self, routes: dict[str, list[StubResponse]]):
        # Each route is a queue of responses, popped in order (for pagination).
        self._routes = {k: list(v) for k, v in routes.items()}
        self.requested_urls: list[str] = []

    class _Request:
        def __init__(self, outer):
            self._outer = outer

        def get(self, url, **kw):
            self._outer.requested_urls.append(url)
            for key, queue in self._outer._routes.items():
                if key in url:
                    return queue.pop(0) if queue else StubResponse(200, {"records": []})
            raise AssertionError(f"Unrouted URL: {url}")

    @property
    def request(self):
        return StubPage._Request(self)


def test_fetch_goals_parses_records():
    page = StubPage({
        "goals": [StubResponse(200, {"records": [
            {"id": 1, "slug": "build-my-tech-skills", "name": "Build my tech skills"},
            {"id": 6, "slug": "start-and-scale-my-business", "name": "Start and scale my business"},
        ]})]
    })
    goals = fetch_goals(page)
    assert [g.slug for g in goals] == ["build-my-tech-skills", "start-and-scale-my-business"]


def test_crawl_listings_paginates_until_exhausted():
    goal = DirectoryGoal(id=6, slug="start-and-scale-my-business", name="Start and scale my business")
    page = StubPage({
        "listings/search": [
            StubResponse(200, {
                "records": [{"id": 1, "slug": "a", "name": "A", "short_description": "d",
                             "human_readable_price": "Free", "price_in_cents": 0}],
                "pagination": {"total_count": 2, "page": 1, "per_page": 1},
            }),
            StubResponse(200, {
                "records": [{"id": 2, "slug": "b", "name": "B", "short_description": "d",
                             "human_readable_price": "Free", "price_in_cents": 0}],
                "pagination": {"total_count": 2, "page": 2, "per_page": 1},
            }),
        ],
    })
    by_id = crawl_listings(page, [goal], request_pause=0)
    assert set(by_id) == {1, 2}
    assert by_id[1].goals == ["start-and-scale-my-business"]


def test_crawl_listings_records_every_goal_a_listing_appears_under():
    goal_a = DirectoryGoal(id=1, slug="goal-a", name="Goal A")
    goal_b = DirectoryGoal(id=2, slug="goal-b", name="Goal B")
    same_listing = {"id": 1, "slug": "a", "name": "A", "short_description": "d",
                     "human_readable_price": "Free", "price_in_cents": 0}
    page = StubPage({
        "listings/search": [
            StubResponse(200, {"records": [same_listing],
                                "pagination": {"total_count": 1, "page": 1, "per_page": 1}}),
            StubResponse(200, {"records": [same_listing],
                                "pagination": {"total_count": 1, "page": 1, "per_page": 1}}),
        ],
    })
    by_id = crawl_listings(page, [goal_a, goal_b], request_pause=0)
    assert by_id[1].goals == ["goal-a", "goal-b"]


def test_crawl_listings_stops_on_empty_page():
    goal = DirectoryGoal(id=1, slug="goal-a", name="Goal A")
    page = StubPage({"listings/search": [StubResponse(200, {"records": [], "pagination": {}})]})
    by_id = crawl_listings(page, [goal], request_pause=0)
    assert by_id == {}


# --- _to_ranked: slug derivation ---------------------------------------------


def test_to_ranked_derives_slug_from_resolved_circle_host():
    listing = DirectoryListing(
        external_id=1, slug="printify-sellers-club", name="Printify Sellers Club",
        description="A seller community", price_label="Free", price_in_cents=0,
        join_url="https://printify.circle.so/join",
    )
    rc = _to_ranked(listing)
    assert rc.slug == "printify"
    assert rc.join_url == "https://printify.circle.so/join"


def test_to_ranked_falls_back_to_discover_slug_when_join_url_unresolved():
    listing = DirectoryListing(
        external_id=1, slug="the-freedom-club", name="First Sale Challenge",
        description="Make your first sale", price_label="Free", price_in_cents=0,
        join_url=None,
    )
    rc = _to_ranked(listing)
    assert rc.slug == "the-freedom-club"
    assert rc.join_url.endswith("/products/the-freedom-club")


def test_to_ranked_uses_external_id_as_slug_when_nothing_else_is_available():
    listing = DirectoryListing(
        external_id=42, slug="", name=None, description=None,
        price_label=None, price_in_cents=None, join_url=None,
    )
    rc = _to_ranked(listing)
    assert rc.slug == "42"


# --- recheck_unresolved_join_urls --------------------------------------------
# resolve_join_url() itself needs a real page.goto()/locator() -- monkeypatched
# here rather than faked, same as circle_leads/join/joiner.py's tests
# monkeypatch attempt_join() instead of faking a browser end to end.


class _FakePlaywrightCM:
    def __enter__(self):
        class _Chromium:
            def launch(self, **kw):
                class _Browser:
                    def new_page(self, **kw):
                        return object()  # never touched -- resolve_join_url is patched

                    def close(self):
                        pass

                return _Browser()

        class _PW:
            chromium = _Chromium()

        return _PW()

    def __exit__(self, *a):
        return False


def _db():
    return Database("sqlite:///" + tempfile.mktemp(suffix=".db"))


def test_recheck_resolves_a_stuck_discover_row(monkeypatch):
    db = _db()
    with db.session() as s:
        c = get_or_create_community(
            s, slug="the-freedom-club",
            url="https://discover.circle.so/products/the-freedom-club",
        )
        c.platform = PLATFORM_DISCOVER

    monkeypatch.setattr(circle_directory, "_require_playwright", lambda: _FakePlaywrightCM)
    monkeypatch.setattr(circle_directory, "resolve_join_url", lambda page, slug, **kw: "https://freedom.circle.so/join")
    monkeypatch.setattr(
        "circle_leads.discovery.discover_communities.detect_platform",
        lambda url, **kw: "circle",
    )
    monkeypatch.setattr(
        "circle_leads.discovery.join_type.fetch_join_classification",
        lambda host, **kw: JoinClassification(JoinType.FREE_JOIN, "public signup"),
    )

    stats = recheck_unresolved_join_urls(db, request_pause=0)

    assert stats == {"total": 1, "resolved": 1, "still_unresolved": 0, "errors": 0}
    with db.session() as s:
        row = s.scalar(select(Community).where(Community.slug == "the-freedom-club"))
        assert row.url == "https://freedom.circle.so/join"
        assert row.platform == "circle"
        assert row.join_type == JoinType.FREE_JOIN


def test_recheck_keeps_the_listing_price_when_the_host_is_private(monkeypatch):
    db = _db()
    with db.session() as s:
        c = get_or_create_community(
            s, slug="skl-club", url="https://discover.circle.so/products/skl-club",
        )
        c.platform = PLATFORM_DISCOVER
        c.price_label = "$29/month"

    monkeypatch.setattr(circle_directory, "_require_playwright", lambda: _FakePlaywrightCM)
    monkeypatch.setattr(circle_directory, "resolve_join_url", lambda page, slug, **kw: "https://members.skl.club/")
    monkeypatch.setattr(
        "circle_leads.discovery.discover_communities.detect_platform",
        lambda url, **kw: "circle",
    )
    monkeypatch.setattr(
        "circle_leads.discovery.join_type.fetch_join_classification",
        lambda host, **kw: JoinClassification(JoinType.LOCKED_UNKNOWN, "HTTP 401 on communities/current"),
    )

    recheck_unresolved_join_urls(db, request_pause=0)

    with db.session() as s:
        row = s.scalar(select(Community).where(Community.slug == "skl-club"))
        assert row.join_type == JoinType.PAID
        assert row.join_type_detail.startswith("price_label fallback: '$29/month'")


def test_recheck_leaves_a_row_alone_when_still_unresolved(monkeypatch):
    db = _db()
    with db.session() as s:
        c = get_or_create_community(
            s, slug="dead-listing",
            url="https://discover.circle.so/products/dead-listing",
        )
        c.platform = PLATFORM_DISCOVER

    monkeypatch.setattr(circle_directory, "_require_playwright", lambda: _FakePlaywrightCM)
    monkeypatch.setattr(circle_directory, "resolve_join_url", lambda page, slug, **kw: None)

    stats = recheck_unresolved_join_urls(db, request_pause=0)

    assert stats == {"total": 1, "resolved": 0, "still_unresolved": 1, "errors": 0}
    with db.session() as s:
        row = s.scalar(select(Community).where(Community.slug == "dead-listing"))
        assert row.platform == PLATFORM_DISCOVER  # untouched
