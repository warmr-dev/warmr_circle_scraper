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
    JoinClassification,
    classify_join_type_pending,
    fetch_join_classification,
    refine_join_classification,
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
    assert c.detail == "private (members only): HTTP 401 on communities/current"
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


def _row(join_type=None, detail=None, price_label=None):
    from types import SimpleNamespace
    return SimpleNamespace(join_type=join_type, join_type_detail=detail, price_label=price_label)


LOCKED = JoinClassification(JoinType.LOCKED_UNKNOWN, "HTTP 401 on communities/current")
NO_ANSWER = JoinClassification(JoinType.UNKNOWN, "HTTP 404")


@pytest.mark.parametrize("live", [LOCKED, NO_ANSWER])
def test_a_paid_label_refines_any_inconclusive_check(live):
    c = refine_join_classification(live, _row(price_label="$99/month"))
    assert c.join_type == JoinType.PAID
    assert c.detail == f"price_label fallback: '$99/month' (live check inconclusive: {live.detail})"


def test_a_free_label_refines_a_check_that_got_no_answer():
    c = refine_join_classification(NO_ANSWER, _row(price_label="Free"))
    assert c.join_type == JoinType.FREE_JOIN


def test_a_free_label_does_not_open_a_members_only_community():
    """Hand check 2026-09-19: 1 of 5 "Free" listings answering 401 was joinable."""
    c = refine_join_classification(LOCKED, _row(price_label="Free"))
    assert c.join_type == JoinType.LOCKED_UNKNOWN
    assert c.detail == ("HTTP 401 on communities/current; directory says 'Free', "
                        "not trusted for a private community")


def test_a_free_label_still_refines_a_403():
    """A 403 is often a custom domain's firewall, not Circle's members-only answer."""
    live = JoinClassification(JoinType.LOCKED_UNKNOWN, "private (members only): HTTP 403 on communities/current")
    assert refine_join_classification(live, _row(price_label="Free")).join_type == JoinType.FREE_JOIN


def test_a_conclusive_live_check_beats_the_listing_price_and_a_manual_verdict():
    live = JoinClassification(JoinType.INVITE_ONLY, "is_private=true")
    row = _row(JoinType.PAID, "manual check 2026-09-19: paid", "$99/month")
    assert refine_join_classification(live, row) is live


@pytest.mark.parametrize("live", [LOCKED, NO_ANSWER])
def test_a_manual_verdict_survives_an_inconclusive_check(live):
    row = _row(JoinType.INVITE_ONLY, "manual check 2026-09-19: invite only", "Free")
    c = refine_join_classification(live, row)
    assert (c.join_type, c.detail) == (JoinType.INVITE_ONLY, "manual check 2026-09-19: invite only")


def test_no_listing_price_leaves_the_live_check_as_it_is():
    assert refine_join_classification(LOCKED, _row()) is LOCKED
    assert refine_join_classification(LOCKED, _row(price_label="  ")) is LOCKED


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


def test_classify_join_type_pending_does_not_trust_a_free_label_on_a_locked_api(monkeypatch):
    """The live API is blocked (401), so the community is private. Discover's
    "Free" label proved wrong for 7 of 10 such rows in the 2026-09-19 hand
    check, so the row stays locked_unknown and the detail keeps the label."""
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
        assert row.join_type == JoinType.LOCKED_UNKNOWN
        assert "directory says 'Free', not trusted" in row.join_type_detail


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


# --- a custom domain Circle no longer serves ----------------------------------
#
# 269 of the 443 communities stuck at "unknown" on 2026-09-24 failed the TLS
# handshake on their own domain, whose DNS still pointed at <slug>.circle.so:
# the community had dropped the custom domain and lives on at the Circle host.


class TlsFailingSession(RoutedStubSession):
    """The custom domain fails the handshake; the Circle host answers."""

    def __init__(self, dead_host, routes):
        super().__init__(routes)
        self._dead = dead_host

    def get(self, url, **kw):
        if self._dead in url:
            import requests
            self.calls.append(url)
            raise requests.exceptions.SSLError("handshake failure")
        return super().get(url, **kw)


FREE_PAYLOAD = {"is_private": False, "allow_signups_to_public_community": True,
                "has_non_draft_paywalls": False, "name": "SaaS Alliance"}


def test_a_dead_custom_domain_is_followed_to_its_circle_host(monkeypatch):
    monkeypatch.setattr(join_type_module, "circle_host_behind",
                        lambda host: "future-of-saas.circle.so")
    session = TlsFailingSession("futureofsaas.io",
                                {"future-of-saas.circle.so": StubResp(200, FREE_PAYLOAD)})
    c = fetch_join_classification("community.futureofsaas.io", session=session)
    assert c.join_type == JoinType.FREE_JOIN
    assert c.moved_to == "future-of-saas.circle.so"
    assert "no longer served" in c.detail


def test_a_dead_custom_domain_with_nothing_behind_it_is_terminal(monkeypatch):
    monkeypatch.setattr(join_type_module, "circle_host_behind", lambda host: None)
    session = TlsFailingSession("gone.example", {})
    c = fetch_join_classification("community.gone.example", session=session)
    assert c.join_type == JoinType.UNKNOWN and c.moved_to is None
    assert c.detail.startswith("custom domain no longer served")


def test_a_circle_host_that_fails_tls_is_not_followed(monkeypatch):
    def boom(host):
        raise AssertionError("no CNAME lookup for a *.circle.so host")

    monkeypatch.setattr(join_type_module, "circle_host_behind", boom)
    c = fetch_join_classification("x.circle.so", session=TlsFailingSession("x.circle.so", {}))
    assert c.detail.startswith("request failed: SSLError")


def _unknown(s, slug, url, *, detail, days_ago=3, icp=False, name="n"):
    from datetime import timedelta

    from circle_leads.storage.models import utcnow

    c = get_or_create_community(s, slug=slug, url=url)
    c.name, c.icp_flag, c.join_type, c.join_type_detail = name, icp, JoinType.UNKNOWN, detail
    c.join_type_checked_at = utcnow() - timedelta(days=days_ago)
    return c


def test_the_scheduled_pass_rechecks_unknowns_it_can_still_change(monkeypatch):
    """The first version took never-checked rows by id -- and every one of the
    443 rows the dashboard counted had been checked once already."""
    db = _db()
    with db.session() as s:
        _unknown(s, "tls", "https://community.tls.io", detail="request failed: SSLError", icp=True)
        _unknown(s, "legacy", "https://legacy.circle.so", detail=None)
        _unknown(s, "fresh", "https://fresh.circle.so", detail="HTTP 500", days_ago=0)
        _unknown(s, "gone", "https://gone.circle.so",
                 detail="host no longer maps to a community (redirects to circle.so marketing site)")
        _unknown(s, "site", "https://www.site.com", detail="non-JSON response")
        get_or_create_community(s, slug="card", url="https://discover.circle.so/products/card")

    seen = []
    monkeypatch.setattr(join_type_module, "fetch_join_classification",
                        lambda host, session=None: seen.append(host) or JoinClassification(
                            JoinType.INVITE_ONLY, "no public signup, no paywall detected"))
    stats = classify_join_type_pending(db, scheduled=True)
    # ICP-fit first; not the fresh answer, the dead ones, or a directory card.
    assert seen == ["community.tls.io", "legacy.circle.so"]
    assert stats["checked"] == 2


def test_a_moved_community_is_repointed_to_its_circle_host(monkeypatch):
    db = _db()
    with db.session() as s:
        _unknown(s, "saas", "https://community.futureofsaas.io",
                 detail="request failed: SSLError", icp=True)
    monkeypatch.setattr(join_type_module, "fetch_join_classification",
                        lambda host, session=None: JoinClassification(
                            JoinType.FREE_JOIN, "open (at future-of-saas.circle.so ...)",
                            moved_to="future-of-saas.circle.so"))
    stats = classify_join_type_pending(db, scheduled=True)
    assert stats["moved"] == 1
    with db.session() as s:
        row = s.scalar(select(Community).where(Community.slug == "saas"))
        assert (row.host, row.url, row.join_type) == (
            "future-of-saas.circle.so", "https://future-of-saas.circle.so", JoinType.FREE_JOIN)


def test_a_moved_community_already_on_file_is_left_as_a_dead_copy(monkeypatch):
    db = _db()
    with db.session() as s:
        _unknown(s, "saas", "https://community.futureofsaas.io",
                 detail="request failed: SSLError", icp=True)
        get_or_create_community(s, slug="future-of-saas", url="https://future-of-saas.circle.so")
    monkeypatch.setattr(join_type_module, "fetch_join_classification",
                        lambda host, session=None: JoinClassification(
                            JoinType.FREE_JOIN, "open", moved_to="future-of-saas.circle.so"))
    classify_join_type_pending(db, scheduled=True)
    with db.session() as s:
        row = s.scalar(select(Community).where(Community.slug == "saas"))
        assert row.host == "community.futureofsaas.io"
        assert row.join_type == JoinType.UNKNOWN
        assert row.join_type_detail.startswith("custom domain no longer served")
