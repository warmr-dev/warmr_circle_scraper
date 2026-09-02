"""Tests for community discovery ranking."""

import pytest

from circle_leads.discovery.finder import (
    RankedCommunity,
    rank_discover_listings,
    rank_extracted,
    rank_from_html,
)
from circle_leads.discovery.discover_communities import DiscoveredCommunity


def test_business_listings_rank_above_hobby_ones():
    listings = [
        {"url_path": "/products/saas-founders", "name": "SaaS Founders",
         "description": "For startup founders building software products"},
        {"url_path": "/products/yoga-circle", "name": "Yoga Circle",
         "description": "Daily yoga and meditation practice"},
    ]
    ranked = rank_discover_listings(listings)
    top = next(c for c in ranked if c.slug == "saas-founders")
    hobby = next(c for c in ranked if c.slug == "yoga-circle")
    assert top.score > hobby.score
    assert top.tier == "STRONG"


def test_free_communities_sort_first_among_equal_relevance():
    listings = [
        {"url_path": "/products/founders-paid", "name": "Founders", "price": "$99/month"},
        {"url_path": "/products/founders-free", "name": "Founders", "price": "Free"},
    ]
    ranked = rank_discover_listings(listings)
    assert ranked[0].is_free is True


def test_free_detection():
    from circle_leads.discovery.finder import is_free

    assert is_free("Free") is True
    assert is_free("From $25/month") is False
    assert is_free(None) is False


def test_rank_extracted_scores_slug_urls():
    communities = [
        DiscoveredCommunity(slug="saas-founders", url="https://saas-founders.circle.so",
                            name="SaaS Founders", description="startup founders"),
        DiscoveredCommunity(slug="knitting", url="https://knitting.circle.so",
                            name="Knitting Circle", description="knitting patterns"),
    ]
    ranked = rank_extracted(communities)
    assert ranked[0].slug == "saas-founders"
    assert ranked[0].score > ranked[-1].score


def test_rank_from_html_reads_slug_links():
    html = '<a href="https://saas-founders.circle.so">SaaS Founders for startups</a>'
    ranked = rank_from_html(html)
    assert ranked
    assert ranked[0].slug == "saas-founders"
    assert ranked[0].join_url == "https://saas-founders.circle.so"


def test_rank_from_html_reads_discover_listing_text():
    """A Discover page names the community in the link text, not the slug."""
    html = (
        '<a href="/products/the-entreprenista-league">'
        'The Entreprenista League — community for women entrepreneurs</a>'
        '<a href="/products/bible-study">Bible Study Co. — daily scripture</a>'
    )
    ranked = rank_from_html(html)
    ent = next(c for c in ranked if c.slug == "the-entreprenista-league")
    bible = next(c for c in ranked if c.slug == "bible-study")
    assert ent.name == "The Entreprenista League"
    assert "entrepreneurs" in ent.reasons
    assert ent.score > bible.score


def test_free_label_in_link_text_is_detected():
    html = '<a href="/products/sandboxx">Sandboxx Community — Free founder network</a>'
    ranked = rank_from_html(html)
    assert ranked[0].is_free is True


def test_discover_infrastructure_paths_are_skipped():
    html = (
        '<a href="/products/real-community">Real Founders Community</a>'
        '<a href="/login">Login</a><a href="/signup">Sign up</a>'
        '<a href="/search">Search</a>'
    )
    slugs = {c.slug for c in rank_from_html(html)}
    assert "real-community" in slugs
    assert not ({"login", "signup", "search"} & slugs)


def test_tiers_map_to_score_bands():
    assert RankedCommunity("x", "X", "u", 60, None, True).tier == "STRONG"
    assert RankedCommunity("x", "X", "u", 40, None, True).tier == "WORTH A LOOK"
    assert RankedCommunity("x", "X", "u", 10, None, True).tier == "PROBABLY NOT"


def test_empty_html_yields_nothing():
    assert rank_from_html("") == []
    assert rank_from_html("<html><body>no communities here</body></html>") == []
