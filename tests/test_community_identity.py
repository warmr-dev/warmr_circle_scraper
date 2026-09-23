"""One host is one community, whatever spelling of its address arrives.

Before the `host` column, `get_or_create_community` matched on an exact url
string, so a community entered from a directory link and again from its bare
address became two rows. Both rows then collected the same posts and both
produced leads -- 121 doubled posts on one community by the time it was found.
"""
from __future__ import annotations

import tempfile

import pytest
from sqlalchemy import func, select

from circle_leads.storage.database import (
    Database,
    community_host,
    get_or_create_community,
)
from circle_leads.storage.models import Community


@pytest.fixture
def session():
    db = Database("sqlite:///" + tempfile.mktemp(suffix=".db"))
    with db.session() as s:
        yield s


DIRECTORY_LINK = (
    "https://community.launchthedamnthing.com/join"
    "?attribution_code=discover&invitation_token=abc-123"
)
BARE = "https://community.launchthedamnthing.com"


def test_directory_link_and_bare_address_are_one_row(session):
    first = get_or_create_community(session, slug="ltdt-discover", url=DIRECTORY_LINK)
    second = get_or_create_community(session, slug="launchthedamnthing", url=BARE)

    assert first.id == second.id
    assert session.scalar(select(func.count()).select_from(Community)) == 1


def test_utm_suffix_is_the_same_community(session):
    a = get_or_create_community(session, slug="practi", url="https://community.practicommunity.com")
    b = get_or_create_community(
        session, slug="practi-discover",
        url="https://community.practicommunity.com/?utm_source=circle_discover",
    )
    assert a.id == b.id


def test_different_hosts_stay_apart(session):
    a = get_or_create_community(session, slug="one", url="https://one.circle.so")
    b = get_or_create_community(session, slug="two", url="https://two.circle.so")
    assert a.id != b.id


def test_directory_host_never_merges_unrelated_communities(session):
    """`discover.circle.so/products/<slug>` lists everyone; the host means nothing."""
    a = get_or_create_community(
        session, slug="cypher", url="https://discover.circle.so/products/cypher")
    b = get_or_create_community(
        session, slug="zaia", url="https://discover.circle.so/products/zaia")

    assert a.id != b.id
    assert a.host is None and b.host is None


def test_host_is_backfilled_on_a_row_written_before_the_column(session):
    session.add(Community(slug="old", url=BARE, host=None))
    session.flush()

    found = get_or_create_community(session, slug="old", url=BARE)
    assert found.host == "community.launchthedamnthing.com"


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://x.circle.so", "x.circle.so"),
        ("https://X.Circle.SO/c/space", "x.circle.so"),
        ("https://www.engglobal.net/", "www.engglobal.net"),
        ("https://discover.circle.so/products/anything", None),
        ("not a url", None),
    ],
)
def test_community_host(url, expected):
    assert community_host(url) == expected
