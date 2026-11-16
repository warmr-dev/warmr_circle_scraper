"""Direct tests for community URL validation (stubbed HTTP, no network)."""

import pytest

from circle_leads.discovery.validate_finds import (
    is_subdomain_community,
    validate_url,
    _extract_price,
)


class StubResp:
    def __init__(self, status, text):
        self.status_code = status
        self.text = text


class StubSession:
    def __init__(self, resp):
        self._resp = resp
    def get(self, url, **kw):
        return self._resp


@pytest.mark.parametrize("url,expected", [
    ("https://saas-founders.circle.so", True),
    ("https://startupandangels.circle.so/c/x", True),
    ("https://discover.circle.so/products/x", False),
    ("https://login.circle.so", False),
    ("https://community.circle.so", False),
    ("https://app.circle.so", False),
    ("https://example.com", False),
])
def test_is_subdomain_community(url, expected):
    assert is_subdomain_community(url) is expected


def test_validate_live_subdomain_ok():
    v = validate_url("https://x.circle.so",
                     session=StubSession(StubResp(200, "<title>X Community</title>")))
    assert v.ok is True


def test_validate_403_is_members_only_not_dead():
    """A 403 on a real community means members-only, which is fine."""
    v = validate_url("https://x.circle.so", session=StubSession(StubResp(403, "")))
    assert v.ok is True


def test_validate_404_is_dead():
    v = validate_url("https://x.circle.so", session=StubSession(StubResp(404, "")))
    assert v.ok is False


def test_validate_discover_category_slug_rejected():
    v = validate_url("https://discover.circle.so/startups",
                     session=StubSession(StubResp(200, "<title>ok</title>")))
    assert v.ok is False
    assert "category" in v.reason.lower() or "seo" in v.reason.lower()


def test_validate_discover_product_page_ok():
    v = validate_url("https://discover.circle.so/products/saasrise",
                     session=StubSession(StubResp(200, "<h2>$197</h2><span>/month</span>")))
    assert v.ok is True


def test_validate_expired_community_dead():
    v = validate_url("https://x.circle.so",
                     session=StubSession(StubResp(200, "Circle plan has expired")))
    assert v.ok is False


def test_extract_price_reads_amount():
    price, is_free = _extract_price("<h2>$197</h2><span>/month</span>")
    assert is_free is False and "197" in price


def test_extract_price_free():
    price, is_free = _extract_price("Join this community for free")
    assert is_free is True


# --- a Discover product page: Join link and price -----------------------------

from circle_leads.discovery.validate_finds import listing_from_html  # noqa: E402
from tests.card_pages import card_html  # noqa: E402


def test_extract_price_ignores_nextjs_references():
    """The free Marketing Talent Exchange card was stored as "$33": a quoted
    reference in the page data, not a price."""
    assert _extract_price(r'{\"d\":\"$33\",\"fill\":\"$26\"}') == (None, None)


def test_listing_reads_the_join_button_and_a_free_offer():
    page = card_html(link="https://community.mte.com/joinus?utm_source=circle_discover&x=1",
                     offers=[(0, None)])
    listing = listing_from_html(page)
    assert listing.join_link == "https://community.mte.com/joinus?utm_source=circle_discover&x=1"
    assert (listing.price_label, listing.is_free) == ("Free", True)


def test_listing_paid_monthly():
    listing = listing_from_html(card_html(
        link="https://community.mte.com/checkout/pro?attribution_code=discover",
        offers=[(19, "MON")]))
    assert (listing.price_label, listing.is_free) == ("$19/month", False)


def test_listing_several_offers_start_from_the_lowest():
    listing = listing_from_html(card_html(offers=[(399, None), (40, "MON")]))
    assert listing.price_label == "From $40/month"


def test_listing_without_a_button_takes_the_external_url():
    listing = listing_from_html(card_html(external="https://www.sales.example/join?a=1&b=2"))
    assert listing.join_link == "https://www.sales.example/join?a=1&b=2"


def test_listing_without_structured_data_has_no_price():
    assert listing_from_html("<html><h2>$197</h2></html>").price_label is None


def test_validate_discover_card_returns_join_link_and_structured_price():
    page = card_html(link="https://community.mte.com/joinus", offers=[(0, None)])
    v = validate_url("https://discover.circle.so/products/mte",
                     session=StubSession(StubResp(200, page)))
    assert v.ok and v.join_link == "https://community.mte.com/joinus"
    assert (v.price, v.is_free) == ("Free", True)
