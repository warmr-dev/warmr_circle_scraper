"""Tests for join-type classification (stubbed HTTP, no network).

Fixture payloads for free_join / paid mirror fields actually observed on
communities/current for founderscupid, startupandangels, being-freelance and
founderstudio (2026-09-10) -- see the join-type design discussion.
"""

import tempfile

import pytest
from sqlalchemy import select

from circle_leads.discovery import join_type as join_type_module
from circle_leads.discovery.join_type import (
    JoinType,
    _classify_payload,
    _price_label_fallback,
    classify_join_type_pending,
    fetch_join_classification,
)
from circle_leads.storage.database import Database, get_or_create_community
from circle_leads.storage.models import Community


class StubResp:
    def __init__(self, status, payload, url=""):
        self.status_code = status
        self._payload = payload
        # Real requests.Response.url after redirects; defaults to "not a
        # marketing-site redirect" so existing locked/unknown tests, which
        # share one stubbed response across both the API and the homepage
        # redirect-check call, keep their prior behavior unless a test opts in.
        self.url = url

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class StubSession:
    def __init__(self, resp):
        self._resp = resp
        self.calls: list[str] = []

    def get(self, url, **kw):
        self.calls.append(url)
        return self._resp


# --- Pure classification from a payload -------------------------------------


def test_private_community_is_invite_only():
    c = _classify_payload({"is_private": True})
    assert c.join_type == JoinType.INVITE_ONLY


def test_subscription_cancelled_overrides_an_open_signup_flag():
    """trigify-social-circle: allow_signups_to_public_community reads true from
    before the operator's Circle plan lapsed, but every visitor now actually
    gets a "Circle plan has expired" page -- confirmed by hand in a browser.
    subscription_cancelled must win regardless of what else the payload says.
    """
    c = _classify_payload({
        "is_private": False,
        "allow_signups_to_public_community": True,
        "has_non_draft_paywalls": False,
        "subscription_cancelled": True,
    })
    assert c.join_type == JoinType.SUBSCRIPTION_EXPIRED


def test_open_signup_is_free_join_even_with_a_paywall():
    """being-freelance: sells paid tiers but the free one is still joinable."""
    c = _classify_payload({
        "is_private": False,
        "allow_signups_to_public_community": True,
        "has_non_draft_paywalls": True,
    })
    assert c.join_type == JoinType.FREE_JOIN


def test_open_signup_no_paywall_is_free_join():
    """founderscupid: open signup, nothing paid at all."""
    c = _classify_payload({
        "is_private": False,
        "allow_signups_to_public_community": True,
        "has_non_draft_paywalls": False,
    })
    assert c.join_type == JoinType.FREE_JOIN


def test_paywall_without_open_signup_is_paid():
    """productizeyourself: no public signup, but sells paid access."""
    c = _classify_payload({
        "is_private": False,
        "allow_signups_to_public_community": False,
        "has_non_draft_paywalls": True,
    })
    assert c.join_type == JoinType.PAID


def test_payment_processor_alone_without_a_live_paywall_is_invite_only():
    """startupandangels: Stripe connected, but no active paywall and no public
    signup -- there is no live way in, paid or free, so it's not "paid"."""
    c = _classify_payload({
        "is_private": False,
        "allow_signups_to_public_community": False,
        "has_non_draft_paywalls": False,
        "has_payment_processor_enabled": True,
    })
    assert c.join_type == JoinType.INVITE_ONLY


def test_no_signup_no_paywall_is_invite_only():
    """startupandangels: closed -- no self-serve path of any kind."""
    c = _classify_payload({
        "is_private": False,
        "allow_signups_to_public_community": False,
        "has_non_draft_paywalls": False,
    })
    assert c.join_type == JoinType.INVITE_ONLY


# --- fetch_join_classification: HTTP status handling ------------------------


@pytest.mark.parametrize("status", [401, 403])
def test_locked_endpoint_is_locked_unknown(status):
    """founderstudio: even communities/current refuses -- can't tell free vs paid."""
    session = StubSession(StubResp(status, None))
    c = fetch_join_classification("founderstudio.circle.so", session=session)
    assert c.join_type == JoinType.LOCKED_UNKNOWN


@pytest.mark.parametrize("status", [401, 403])
def test_locked_endpoint_that_redirects_to_marketing_site_is_unknown(status):
    """surferseo: communities/current also 401s, but the plain page redirects
    to circle.so -- the host doesn't map to any community anymore, so this
    must not be filed as "locked but real" like test_locked_endpoint_is_locked_unknown."""
    session = StubSession(StubResp(status, None, url="https://circle.so/"))
    c = fetch_join_classification("surferseo.circle.so", session=session)
    assert c.join_type == JoinType.UNKNOWN
    assert "no longer maps to a community" in c.detail


def test_404_is_unknown():
    session = StubSession(StubResp(404, None))
    c = fetch_join_classification("dead.circle.so", session=session)
    assert c.join_type == JoinType.UNKNOWN


def test_non_json_body_is_unknown():
    session = StubSession(StubResp(200, None))  # .json() raises
    c = fetch_join_classification("notcircle.example.com", session=session)
    assert c.join_type == JoinType.UNKNOWN


def test_missing_expected_field_is_unknown():
    session = StubSession(StubResp(200, {"unrelated": "field"}))
    c = fetch_join_classification("weird.circle.so", session=session)
    assert c.join_type == JoinType.UNKNOWN


def test_request_exception_is_unknown():
    class RaisingSession:
        def get(self, url, **kw):
            import requests
            raise requests.RequestException("boom")

    c = fetch_join_classification("timeout.circle.so", session=RaisingSession())
    assert c.join_type == JoinType.UNKNOWN


def test_happy_path_end_to_end():
    session = StubSession(StubResp(200, {
        "is_private": False,
        "allow_signups_to_public_community": True,
        "has_non_draft_paywalls": False,
    }))
    c = fetch_join_classification("https://founderscupid.circle.so/", session=session)
    assert c.join_type == JoinType.FREE_JOIN
    # Scheme/trailing slash stripped before building the request URL.
    assert session.calls == [
        "https://founderscupid.circle.so/internal_api/communities/current"
    ]


def test_empty_host_is_unknown():
    c = fetch_join_classification("", session=StubSession(StubResp(200, {})))
    assert c.join_type == JoinType.UNKNOWN


# --- fetch_join_classification: www retry when the apex is UNKNOWN ----------


def test_www_retry_recovers_when_apex_404s_but_www_answers():
    """bravelybeingyou.com / grocommunity.se, confirmed by hand: the apex 404s
    on this exact path with no redirect, but www.<host> answers normally."""
    session = RoutedStubSession({
        "https://bare.example.com/internal_api": StubResp(404, None),
        "https://www.bare.example.com/internal_api": StubResp(200, {
            "is_private": False,
            "allow_signups_to_public_community": True,
            "has_non_draft_paywalls": False,
        }),
    })
    c = fetch_join_classification("bare.example.com", session=session)
    assert c.join_type == JoinType.FREE_JOIN
    assert "www retry" in c.detail
    assert "HTTP 404" in c.detail


def test_www_retry_is_not_attempted_for_locked_unknown():
    """A 401/403 already proves something real is there -- retrying www must
    not fire (creativeleader.net stays locked on both apex and www)."""
    session = RoutedStubSession({"locked.example.com": StubResp(401, None)})
    c = fetch_join_classification("locked.example.com", session=session)
    assert c.join_type == JoinType.LOCKED_UNKNOWN
    assert c.detail == "HTTP 401 on communities/current"
    # One call for the API check, one for the marketing-site redirect check --
    # neither is a www retry (the point of this test).
    assert session.calls == [
        "https://locked.example.com/internal_api/communities/current",
        "https://locked.example.com/",
    ]


def test_www_retry_gives_up_when_www_is_also_unknown():
    session = RoutedStubSession({"internal_api": StubResp(404, None)})
    c = fetch_join_classification("still-dead.example.com", session=session)
    assert c.join_type == JoinType.UNKNOWN
    assert c.detail == "HTTP 404"  # unchanged -- no retry noise when it didn't help
    assert len(session.calls) == 2  # apex, then the www retry


def test_www_retry_is_not_attempted_when_host_is_already_www():
    session = RoutedStubSession({"internal_api": StubResp(404, None)})
    c = fetch_join_classification("www.already-www.example.com", session=session)
    assert c.join_type == JoinType.UNKNOWN
    assert len(session.calls) == 1


# --- _price_label_fallback: Discover's own price tag as a backup signal -----


@pytest.mark.parametrize("label", ["Free", "FREE", "$0", "From $0", "Free trial"])
def test_price_label_fallback_recognizes_free(label):
    c = _price_label_fallback(label)
    assert c.join_type == JoinType.FREE_JOIN


@pytest.mark.parametrize("label", ["$29/month", "From $9.99/month", "$3,500"])
def test_price_label_fallback_recognizes_paid(label):
    c = _price_label_fallback(label)
    assert c.join_type == JoinType.PAID


@pytest.mark.parametrize("label", [None, "", "   "])
def test_price_label_fallback_is_none_without_a_real_label(label):
    assert _price_label_fallback(label) is None


# --- classify_join_type_pending: the never-checked backlog -------------------


class RoutedStubSession:
    """Like StubSession, but routes by host substring -- a real backfill run
    hits many different communities, each needing its own fixture payload."""

    def __init__(self, by_host_substring: dict[str, "StubResp"]):
        self._routes = by_host_substring
        self.calls: list[str] = []

    def get(self, url, **kw):
        self.calls.append(url)
        for key, resp in self._routes.items():
            if key in url:
                return resp
        raise AssertionError(f"Unrouted URL in test: {url}")


def _db():
    return Database("sqlite:///" + tempfile.mktemp(suffix=".db"))


def test_classify_join_type_pending_only_touches_never_checked_rows(monkeypatch):
    db = _db()
    with db.session() as s:
        get_or_create_community(s, slug="free-one", url="https://free-one.circle.so")
        paid = get_or_create_community(s, slug="paid-one", url="https://paid-one.circle.so")
        already = get_or_create_community(s, slug="already-checked", url="https://already.circle.so")
        already.join_type = JoinType.INVITE_ONLY
        from circle_leads.storage.models import utcnow
        already.join_type_checked_at = utcnow()

    session = RoutedStubSession({
        "free-one": StubResp(200, {"is_private": False,
                                    "allow_signups_to_public_community": True,
                                    "has_non_draft_paywalls": False}),
        "paid-one": StubResp(200, {"is_private": False,
                                    "allow_signups_to_public_community": False,
                                    "has_non_draft_paywalls": True}),
    })
    monkeypatch.setattr(join_type_module, "shared_session", lambda: session)

    stats = classify_join_type_pending(db)

    assert stats["checked"] == 2  # not the already-checked row
    assert stats[JoinType.FREE_JOIN] == 1
    assert stats[JoinType.PAID] == 1

    with db.session() as s:
        free = s.scalar(select(Community).where(Community.slug == "free-one"))
        paid_row = s.scalar(select(Community).where(Community.slug == "paid-one"))
        assert free.join_type == JoinType.FREE_JOIN
        assert free.join_type_checked_at is not None
        assert paid_row.join_type == JoinType.PAID


def test_classify_join_type_pending_respects_limit(monkeypatch):
    db = _db()
    with db.session() as s:
        get_or_create_community(s, slug="a", url="https://a.circle.so")
        get_or_create_community(s, slug="b", url="https://b.circle.so")

    session = RoutedStubSession({"": StubResp(200, {"is_private": True})})
    monkeypatch.setattr(join_type_module, "shared_session", lambda: session)

    stats = classify_join_type_pending(db, limit=1)
    assert stats["checked"] == 1


def test_classify_join_type_pending_saves_the_reason_not_just_the_bucket(monkeypatch):
    db = _db()
    with db.session() as s:
        get_or_create_community(s, slug="dead-one", url="https://dead-one.circle.so")

    session = RoutedStubSession({"dead-one": StubResp(404, None)})
    monkeypatch.setattr(join_type_module, "shared_session", lambda: session)

    classify_join_type_pending(db)

    with db.session() as s:
        row = s.scalar(select(Community).where(Community.slug == "dead-one"))
        assert row.join_type == JoinType.UNKNOWN
        assert row.join_type_detail == "HTTP 404"


def test_classify_join_type_pending_falls_back_to_price_label_when_api_is_locked(monkeypatch):
    """A custom domain from circle_directory: the live API is blocked (401),
    but Discover's own listing already said "Free" at crawl time -- that
    shouldn't stay stuck at locked_unknown when the price tag is sitting
    right there in the row."""
    db = _db()
    with db.session() as s:
        get_or_create_community(
            s, slug="priced-free", url="https://priced-free.example.com", price_label="Free",
        )

    session = RoutedStubSession({"priced-free": StubResp(401, None)})
    monkeypatch.setattr(join_type_module, "shared_session", lambda: session)

    classify_join_type_pending(db)

    with db.session() as s:
        row = s.scalar(select(Community).where(Community.slug == "priced-free"))
        assert row.join_type == JoinType.FREE_JOIN
        assert "price_label fallback: 'Free'" in row.join_type_detail
        assert "live check inconclusive" in row.join_type_detail


def test_classify_join_type_pending_price_label_fallback_handles_paid(monkeypatch):
    db = _db()
    with db.session() as s:
        get_or_create_community(
            s, slug="priced-paid", url="https://priced-paid.example.com", price_label="$29/month",
        )

    session = RoutedStubSession({"priced-paid": StubResp(404, None)})
    monkeypatch.setattr(join_type_module, "shared_session", lambda: session)

    classify_join_type_pending(db)

    with db.session() as s:
        row = s.scalar(select(Community).where(Community.slug == "priced-paid"))
        assert row.join_type == JoinType.PAID


def test_classify_join_type_pending_ignores_price_label_when_api_is_decisive(monkeypatch):
    """A price_label must never override a live check that actually answered --
    it's a fallback for UNKNOWN/LOCKED_UNKNOWN only."""
    db = _db()
    with db.session() as s:
        get_or_create_community(
            s, slug="decisive", url="https://decisive.circle.so", price_label="$99/month",
        )

    session = RoutedStubSession({
        "decisive": StubResp(200, {"is_private": True}),  # -> invite_only
    })
    monkeypatch.setattr(join_type_module, "shared_session", lambda: session)

    classify_join_type_pending(db)

    with db.session() as s:
        row = s.scalar(select(Community).where(Community.slug == "decisive"))
        assert row.join_type == JoinType.INVITE_ONLY


def test_classify_join_type_pending_recheck_touches_already_checked_rows(monkeypatch):
    db = _db()
    from circle_leads.storage.models import utcnow
    with db.session() as s:
        already = get_or_create_community(s, slug="already-checked", url="https://already.circle.so")
        already.join_type = JoinType.INVITE_ONLY
        already.join_type_checked_at = utcnow()

    session = RoutedStubSession({
        "already": StubResp(200, {"is_private": False,
                                   "allow_signups_to_public_community": True,
                                   "has_non_draft_paywalls": False}),
    })
    monkeypatch.setattr(join_type_module, "shared_session", lambda: session)

    stats = classify_join_type_pending(db, recheck=True)

    assert stats["checked"] == 1
    with db.session() as s:
        row = s.scalar(select(Community).where(Community.slug == "already-checked"))
        assert row.join_type == JoinType.FREE_JOIN  # overwritten by the recheck
