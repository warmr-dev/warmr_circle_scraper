"""Tests for persisting search finds and flagging new ones.

Validation hits the network, so these unit tests pass validate=False and cover
validation separately with a stubbed session.
"""

import pytest

from circle_leads.discovery.finder import RankedCommunity
from circle_leads.discovery.persist import new_since, persist_finds
from circle_leads.storage.database import Database

# Subdomain finds get a +15 boost in persist; account for it in expectations.
SUBDOMAIN_BOOST = 15


@pytest.fixture
def db(tmp_path):
    return Database(f"sqlite:///{tmp_path}/persist.db")


def rc(slug, score, name=None, url=None, free=False):
    return RankedCommunity(
        slug=slug, name=name or slug, join_url=url or f"https://{slug}.circle.so",
        score=score, price_label="Free" if free else None, is_free=free,
        reasons=["founder"],
    )


def save(db, finds, **kw):
    kw.setdefault("validate", False)
    return persist_finds(db, finds, **kw)


def test_first_run_flags_everything_new(db):
    result = save(db, [rc("a", 60), rc("b", 40)], niche="founders", min_score=25)
    assert result.new_count == 2
    assert {c.slug for c in result.new} == {"a", "b"}


def test_second_run_flags_nothing_new(db):
    finds = [rc("a", 60), rc("b", 40)]
    save(db, finds, niche="founders", min_score=25)
    second = save(db, finds, niche="founders", min_score=25)
    assert second.new_count == 0
    assert second.unchanged == 2


def test_only_genuinely_new_slugs_are_flagged(db):
    save(db, [rc("a", 60)], niche="founders", min_score=25)
    second = save(db, [rc("a", 60), rc("c", 50)], niche="founders", min_score=25)
    assert {x.slug for x in second.new} == {"c"}


def test_min_score_filters_finds(db):
    result = save(db, [rc("a", 60), rc("low", 10)], min_score=25)
    assert result.new_count == 1
    assert new_since(db) and all(r["slug"] != "low" for r in new_since(db))


def test_improved_score_updates_but_does_not_reflag(db):
    save(db, [rc("a", 40)], min_score=25)
    second = save(db, [rc("a", 70)], min_score=25)
    assert second.new_count == 0
    assert len(second.updated) == 1
    assert new_since(db)[0]["score"] == 70 + SUBDOMAIN_BOOST


def test_new_since_lists_unvisited_best_first(db):
    save(db, [rc("a", 40), rc("b", 80), rc("c", 60)], min_score=25)
    rows = new_since(db)
    assert [r["slug"] for r in rows] == ["b", "c", "a"]


def test_subdomain_finds_are_boosted_over_discover(db):
    """A real <slug>.circle.so community outranks a same-score Discover listing."""
    save(db, [
        rc("sub", 50, url="https://sub.circle.so"),
        rc("disc", 50, url="https://discover.circle.so/products/disc"),
    ], min_score=25)
    rows = {r["slug"]: r["score"] for r in new_since(db)}
    assert rows["sub"] > rows["disc"]


def test_find_never_implies_approval(db):
    from sqlalchemy import select

    from circle_leads.storage.models import Community

    save(db, [rc("a", 60)], min_score=25)
    with db.session() as s:
        c = s.scalar(select(Community).where(Community.slug == "a"))
        assert c.permission_status == "candidate"
        assert c.access_status == "not_visited"
        assert c.is_ingestable is False


def test_persist_logs_activity(db):
    from circle_leads.storage.activity import recent_activity

    save(db, [rc("a", 60)], niche="flutter", min_score=25)
    with db.session() as s:
        events = recent_activity(s, kind="discover")
    assert events
    assert "flutter" in events[0]["summary"]


# --- Validation (stubbed session, no network) -------------------------------


class StubResp:
    def __init__(self, status, text):
        self.status_code = status
        self.text = text


class StubSession:
    def __init__(self, mapping):
        self.mapping = mapping

    def get(self, url, **kw):
        for key, resp in self.mapping.items():
            if key in url:
                return resp
        return StubResp(200, "<title>ok</title>")


def test_validation_drops_discover_category_slugs(db):
    """Discover is an SPA, so bare category slugs are dropped by path, not body;
    only /products/<slug> listings are kept."""
    session = StubSession({
        "startups": StubResp(200, "<title>ok</title>"),
        "products/saasrise": StubResp(200, "<title>ok</title>"),
    })
    result = persist_finds(
        db,
        [
            rc("startups", 60, url="https://discover.circle.so/startups"),
            rc("saasrise", 60, url="https://discover.circle.so/products/saasrise"),
        ],
        min_score=25, validate=True, session=session,
    )
    slugs = {c.slug for c in result.new}
    assert "saasrise" in slugs
    assert "startups" not in slugs


def test_validation_keeps_live_subdomains(db):
    session = StubSession({
        "startupandangels": StubResp(200, "<title>Startup&Angels community</title>"),
    })
    result = persist_finds(
        db, [rc("startupandangels", 30, url="https://startupandangels.circle.so")],
        min_score=25, validate=True, session=session,
    )
    assert result.new_count == 1


# --- Price detection / paid deprioritization --------------------------------

def test_extract_price_reads_discover_product_html():
    from circle_leads.discovery.validate_finds import _extract_price
    html = '<h2 class="text-heading-2xl">$197</h2><span>/month</span>'
    price, is_free = _extract_price(html)
    assert is_free is False
    assert "197" in price


def test_extract_price_reports_free_when_no_price():
    from circle_leads.discovery.validate_finds import _extract_price
    price, is_free = _extract_price("<div>Join this community for free</div>")
    assert is_free is True
    assert price == "Free"


def test_paid_communities_are_deprioritized(db):
    class R:
        def __init__(self, s, t): self.status_code, self.text = s, t
    class Sess:
        def get(self, url, **kw):
            if "paid" in url:
                return R(200, '<h2>$197</h2><span>/month</span>')
            return R(200, '<title>ok</title> free to join')
    result = persist_finds(
        db,
        [
            rc("paidcomm", 60, url="https://discover.circle.so/products/paidcomm"),
            rc("freecomm", 60, url="https://discover.circle.so/products/freecomm"),
        ],
        min_score=10, validate=True, session=Sess(),
    )
    scores = {r["slug"]: r["score"] for r in new_since(db)}
    assert scores["freecomm"] > scores["paidcomm"]
