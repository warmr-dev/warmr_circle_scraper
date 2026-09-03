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
