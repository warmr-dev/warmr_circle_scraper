"""Tests for join-type classification (stubbed HTTP, no network).

Fixture payloads for free_join / paid mirror fields actually observed on
communities/current for founderscupid, startupandangels, being-freelance and
founderstudio (2026-09-10) -- see the join-type design discussion.
"""

import pytest

from circle_leads.discovery.join_type import (
    JoinType,
    _classify_payload,
    fetch_join_classification,
)


class StubResp:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload

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
