"""Discover cards get their community's address (stubbed HTTP, no network)."""

import pytest
import requests
from sqlalchemy import select

from circle_leads.discovery.directory_cards import NOTE_PREFIX, resolve_directory_cards
from circle_leads.storage.database import Database
from circle_leads.storage.models import Community
from tests.card_pages import card_html

CARD = "https://discover.circle.so/products/"


class Resp:
    def __init__(self, status=200, text="", content_type="text/html"):
        self.status_code = status
        self.text = text
        self.headers = {"content-type": content_type}


class Session:
    """Answers by URL substring; a value that is an exception is raised."""

    def __init__(self, mapping):
        self.mapping = mapping
        self.calls: list[str] = []

    def get(self, url, **kw):
        self.calls.append(url)
        for key, value in self.mapping.items():
            if key in url:
                if isinstance(value, Exception):
                    raise value
                return value
        return Resp(200, "<html>not circle</html>")


CIRCLE_API = Resp(200, '{"records": []}', "application/json")


@pytest.fixture
def db(tmp_path):
    return Database(f"sqlite:///{tmp_path}/cards.db")


def add(db, slug, url, **kw):
    with db.session() as s:
        row = Community(slug=slug, url=url, **kw)
        s.add(row)
        s.flush()
        return row.id


def get(db, community_id):
    with db.session() as s:
        row = s.get(Community, community_id)
        s.expunge(row)
        return row


def run(db, session, **kw):
    return resolve_directory_cards(db, session=session, pause=0, **kw)


def test_card_leading_to_a_circle_community_takes_its_address(db):
    cid = add(db, "mte", CARD + "mte", platform="discover", price_label="$33",
              join_type="paid", icp_flag=True)
    session = Session({
        CARD + "mte": Resp(200, card_html(
            link="https://community.mte.com/joinus?utm_source=circle_discover",
            offers=[(0, None)])),
        "community.mte.com/internal_api/spaces": CIRCLE_API,
    })
    stats = run(db, session)
    row = get(db, cid)
    assert stats["resolved"] == 1
    assert (row.host, row.url, row.platform) == (
        "community.mte.com", "https://community.mte.com", "circle")
    assert row.price_label == "Free"
    # The next join-type pass asks Circle about it.
    assert row.join_type_checked_at is None
    assert row.notes == f"{NOTE_PREFIX} found as {CARD}mte"


def test_circle_so_subdomain_needs_no_probe(db):
    cid = add(db, "nb", CARD + "nb")
    session = Session({CARD + "nb": Resp(200, card_html(
        link="https://jessicaswift.circle.so/checkout/nb?attribution_code=discover",
        offers=[(10, "MON")]))})
    run(db, session)
    assert get(db, cid).host == "jessicaswift.circle.so"
    assert session.calls == [CARD + "nb"]


def test_card_for_a_community_we_already_hold_is_a_duplicate(db):
    holder = add(db, "mte-home", "https://community.mte.com", host="community.mte.com")
    cid = add(db, "mte-paid", CARD + "mte-paid")
    session = Session({
        CARD + "mte-paid": Resp(200, card_html(
            link="https://community.mte.com/checkout/pro", offers=[(19, "MON")])),
        "community.mte.com/internal_api/spaces": CIRCLE_API,
    })
    stats = run(db, session)
    row = get(db, cid)
    assert stats["duplicate"] == 1
    assert row.host is None
    assert row.notes == f"{NOTE_PREFIX} same community as #{holder} (community.mte.com)"


def test_address_already_used_as_a_url_keeps_the_link(db):
    add(db, "old", "https://community.mte.com")      # url, but never given a host
    cid = add(db, "mte", CARD + "mte")
    session = Session({
        CARD + "mte": Resp(200, card_html(link="https://community.mte.com/joinus")),
        "community.mte.com/internal_api/spaces": CIRCLE_API,
    })
    run(db, session)
    row = get(db, cid)
    assert (row.host, row.url) == ("community.mte.com", "https://community.mte.com/joinus")


def test_card_leading_off_circle_is_noted_and_not_read_again(db):
    cid = add(db, "boss", CARD + "boss", platform="discover")
    session = Session({CARD + "boss": Resp(200, card_html(
        link="https://www.sales.example/bosstable", offers=[(125, "MON")]))})
    assert run(db, session)["not_circle"] == 1
    row = get(db, cid)
    assert row.host is None and row.platform == "other"
    assert row.notes.startswith(f"{NOTE_PREFIX} its Join button leads off Circle")
    assert run(db, session)["checked"] == 0


def test_listing_gone_is_noted(db):
    cid = add(db, "gone", CARD + "gone")
    assert run(db, Session({CARD + "gone": Resp(404, "")}))["gone"] == 1
    assert get(db, cid).notes.startswith(f"{NOTE_PREFIX} listing gone (HTTP 404)")


def test_listing_without_a_link_is_noted(db):
    cid = add(db, "bare", CARD + "bare")
    stats = run(db, Session({CARD + "bare": Resp(200, card_html(offers=[(49, None)]))}))
    row = get(db, cid)
    assert stats["no_link"] == 1
    assert row.price_label == "$49"
    assert row.notes == f"{NOTE_PREFIX} the listing shows no Join link"


def test_a_failed_fetch_goes_to_the_back_of_the_line(db):
    first = add(db, "a", CARD + "a")
    second = add(db, "b", CARD + "b")
    session = Session({
        CARD + "a": requests.ConnectionError("down"),
        CARD + "b": Resp(200, card_html(link="https://b.circle.so/join")),
    })
    assert run(db, session, limit=1)["errors"] == 1
    assert get(db, first).notes is None             # not settled: tried again later
    run(db, session, limit=1)
    assert get(db, second).host == "b.circle.so"     # the next pass took the other card


def test_icp_cards_first(db):
    add(db, "plain", CARD + "plain", icp_flag=False)
    icp = add(db, "fit", CARD + "fit", icp_flag=True)
    session = Session({CARD + "fit": Resp(200, card_html(link="https://fit.circle.so"))})
    run(db, session, limit=1)
    assert session.calls == [CARD + "fit"]
    assert get(db, icp).host == "fit.circle.so"


def test_rows_with_an_address_are_left_alone(db):
    add(db, "real", "https://real.circle.so", host="real.circle.so")
    session = Session({})
    assert run(db, session)["checked"] == 0
    assert session.calls == []


def test_settled_cards_are_not_rows_that_still_look_like_cards(db):
    """The two kinds of row the pass skips are the ones its notes settled."""
    add(db, "a", CARD + "a", notes=f"{NOTE_PREFIX} listing gone (HTTP 404) 2026-11-16")
    add(db, "b", CARD + "b", notes="a person's own note")
    session = Session({CARD + "b": Resp(200, card_html(link="https://b.circle.so"))})
    run(db, session)
    with db.session() as s:
        b = s.scalar(select(Community).where(Community.slug == "b"))
        assert b.host == "b.circle.so"
        assert b.notes == f"{NOTE_PREFIX} found as {CARD}b\na person's own note"
    assert session.calls == [CARD + "b"]
