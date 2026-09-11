"""Classify how a Circle community can be joined -- free, paid, invite-only, or
undetectable -- so the harvest can flag which account (test vs. main) should
join it.

One unauthenticated JSON call answers this for any Circle host, subdomain or
custom domain: ``/internal_api/communities/current``. Same public API family
``harvest.py`` and ``public_reader.py`` already read (not behind Cloudflare) --
no browser, no login, no cookie.

This does not resolve an exact price: ``/internal_api/paywalls`` requires
membership, so a paid community's price still comes from its Discover listing
(``discovery/validate_finds.py``) when it has one. This only answers the
yes/no that decides which account, if any, should join.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import requests

from circle_leads.scraper.http_client import BROWSER_UA, shared_session

logger = logging.getLogger(__name__)


class JoinType:
    """How a community can be joined, from most to least accessible."""

    FREE_JOIN = "free_join"            # public self-signup, no payment required
    PAID = "paid"                      # signup requires payment; no free tier is public
    INVITE_ONLY = "invite_only"        # no public signup at all (private or closed)
    LOCKED_UNKNOWN = "locked_unknown"  # even the public metadata call is refused
    UNKNOWN = "unknown"                # not a reachable/recognizable Circle host

    ALL = (FREE_JOIN, PAID, INVITE_ONLY, LOCKED_UNKNOWN, UNKNOWN)


@dataclass
class JoinClassification:
    join_type: str
    detail: str = ""


def _classify_payload(data: dict) -> JoinClassification:
    """Pure classification from an already-fetched communities/current payload.

    Order matters: a free tier being joinable outranks a paywall also being
    present -- ``being-freelance``, ``talentcollective`` and others sell paid
    tiers but still let anyone sign up for the free one, which is what the
    test account can use.
    """
    if data.get("is_private"):
        return JoinClassification(JoinType.INVITE_ONLY, "is_private=true")
    if data.get("allow_signups_to_public_community"):
        return JoinClassification(
            JoinType.FREE_JOIN, "allow_signups_to_public_community=true"
        )
    if data.get("has_non_draft_paywalls"):
        # NOT has_payment_processor_enabled alone -- that only means Stripe is
        # connected to the account, not that this community currently sells a
        # tier (measured on startupandangels: processor enabled, no non-draft
        # paywall, no public signup -- there is no live way in at all, so
        # invite_only is the honest label, not "go pay for it").
        return JoinClassification(JoinType.PAID, "paywall present, no public free signup")
    # No paywall and no open signup -- closed: invite link, application, or a
    # community that only accepts members through some other gate.
    return JoinClassification(JoinType.INVITE_ONLY, "no public signup, no paywall detected")


def fetch_join_classification(
    host: str, *, session: requests.Session | None = None, timeout: int = 12
) -> JoinClassification:
    """Classify one host's join requirement. Never raises.

    A 401/403 on this metadata call is its own outcome (``locked_unknown``):
    the community is locked down even further than a private space list, and
    free vs. paid genuinely can't be told apart without a human on the join
    page.
    """
    http = session or shared_session()
    host = (host or "").replace("https://", "").replace("http://", "").strip("/")
    if not host:
        return JoinClassification(JoinType.UNKNOWN, "empty host")
    try:
        resp = http.get(
            f"https://{host}/internal_api/communities/current",
            headers={"User-Agent": BROWSER_UA, "Accept": "application/json"},
            timeout=timeout,
        )
    except requests.RequestException as exc:
        return JoinClassification(JoinType.UNKNOWN, f"request failed: {exc.__class__.__name__}")

    if resp.status_code in (401, 403):
        return JoinClassification(
            JoinType.LOCKED_UNKNOWN, f"HTTP {resp.status_code} on communities/current"
        )
    if resp.status_code != 200:
        return JoinClassification(JoinType.UNKNOWN, f"HTTP {resp.status_code}")

    try:
        data = resp.json()
    except ValueError:
        return JoinClassification(JoinType.UNKNOWN, "non-JSON response")
    if not isinstance(data, dict) or "is_private" not in data:
        return JoinClassification(JoinType.UNKNOWN, "response missing expected fields")

    return _classify_payload(data)
