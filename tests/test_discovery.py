import pytest

from circle_leads.discovery.discover_communities import (
    dedupe,
    extract_from_text,
    normalize_community_url,
)
from circle_leads.discovery.validate_community import assess_relevance


@pytest.mark.parametrize(
    "raw,expected_slug",
    [
        ("https://startup-founders.circle.so", "startup-founders"),
        ("startup-founders.circle.so", "startup-founders"),
        ("https://devs.circle.so/c/general/post-123", "devs"),
        ("HTTPS://Devs.Circle.SO/", "devs"),
    ],
)
def test_normalize_valid_urls(raw, expected_slug):
    result = normalize_community_url(raw)
    assert result is not None
    assert result[0] == expected_slug


@pytest.mark.parametrize(
    "raw",
    [
        "https://example.com",
        "https://app.circle.so/settings",   # reserved infrastructure host
        "https://discover.circle.so",       # directory, not a community
        "https://circle.so/pricing",
        "",
        "not a url",
    ],
)
def test_normalize_rejects_non_communities(raw):
    assert normalize_community_url(raw) is None


def test_extract_finds_communities_in_public_html():
    html = """
      <a href="https://startup-founders.circle.so">Founders</a>
      <a href="https://saas-builders.circle.so/c/jobs">SaaS Builders</a>
      <a href="https://app.circle.so/login">Login</a>
      <a href="https://unrelated.com">Other</a>
    """
    found = extract_from_text(html, source="test")
    slugs = sorted(c.slug for c in found)
    assert slugs == ["saas-builders", "startup-founders"]


def test_extract_deduplicates_repeated_links():
    html = "a.circle.so a.circle.so/c/x https://a.circle.so/posts/1"
    assert len(extract_from_text(html)) == 1


def test_dedupe_merges_metadata():
    from circle_leads.discovery.discover_communities import DiscoveredCommunity

    merged = dedupe([
        DiscoveredCommunity(slug="a", url="https://a.circle.so", name="A"),
        DiscoveredCommunity(slug="a", url="https://a.circle.so", description="desc"),
    ])
    assert len(merged) == 1
    assert merged[0].name == "A"
    assert merged[0].description == "desc"


def test_business_community_is_relevant():
    a = assess_relevance("Startup Founders", "A community for SaaS founders and entrepreneurs building tech products")
    assert a.relevant
    assert "startup" in a.reasons


def test_hobby_community_is_not_relevant():
    a = assess_relevance("Knitting Circle", "Share knitting patterns and recipes with fellow crafters")
    assert not a.relevant


def test_hiring_space_boosts_relevance():
    with_jobs = assess_relevance("Dev Community", "Software developers. Jobs board and hiring channel.")
    without = assess_relevance("Dev Community", "Software developers chatting.")
    assert with_jobs.score > without.score


def test_empty_metadata_scores_zero():
    assert assess_relevance(None, None).score == 0


# --- Bare hostnames without a scheme ---------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("visit foo.circle.so", ["foo"]),
        ("baz.circle.so/c/jobs", ["baz"]),
        ("Join qux.circle.so!", ["qux"]),
        ("(quux.circle.so)", ["quux"]),
        ("Community: my-startup.circle.so.", ["my-startup"]),
        ("http://legacy.circle.so", ["legacy"]),
    ],
)
def test_extracts_bare_hostnames_without_a_scheme(text, expected):
    """People write "foo.circle.so" in prose far more often than a full URL."""
    assert [c.slug for c in extract_from_text(text)] == expected


@pytest.mark.parametrize(
    "text",
    [
        "dana@app.circle.so",       # email domain, not a link
        "sub.domain.circle.so",     # deeper hostname, not a community slug
        "discover.circle.so",       # directory, reserved
        "notcircle.so",
        "xcircle.so",
    ],
)
def test_bare_hostname_matching_does_not_over_capture(text):
    assert extract_from_text(text) == []
