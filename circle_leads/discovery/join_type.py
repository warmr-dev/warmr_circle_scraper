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
from urllib.parse import urlparse

import requests

from circle_leads.scraper.governor import LOW, priority
from circle_leads.scraper.http_client import BROWSER_UA, shared_session

logger = logging.getLogger(__name__)


class JoinType:
    """How a community can be joined, from most to least accessible."""

    FREE_JOIN = "free_join"            # public self-signup, no payment required
    PAID = "paid"                      # signup requires payment; no free tier is public
    INVITE_ONLY = "invite_only"        # no public signup at all (private or closed)
    LOCKED_UNKNOWN = "locked_unknown"  # even the public metadata call is refused
    UNKNOWN = "unknown"                # not a reachable/recognizable Circle host
    SUBSCRIPTION_EXPIRED = "subscription_expired"  # operator's own Circle plan
    # lapsed -- nobody can get in, member or not, until they pay Circle again

    ALL = (FREE_JOIN, PAID, INVITE_ONLY, LOCKED_UNKNOWN, UNKNOWN, SUBSCRIPTION_EXPIRED)


def _redirects_to_marketing_site(
    host: str, *, session: requests.Session, timeout: int
) -> bool:
    """True if the bare host no longer maps to any community at all.

    When Circle's router has nothing to serve for a hostname, it falls
    through to the circle.so marketing site rather than 404ing -- and
    ``communities/current`` still answers 401 for that same host, identical
    to a real locked/private community (confirmed by hand on
    ``surferseo.circle.so``: 401 on the API, but the plain page redirects to
    ``circle.so``). This disambiguates "nothing here" from "something here,
    but locked" so a dead slug doesn't get filed as ``locked_unknown``.
    """
    try:
        resp = session.get(
            f"https://{host}/",
            headers={"User-Agent": BROWSER_UA},
            timeout=timeout,
            allow_redirects=True,
        )
    except requests.RequestException:
        return False
    return (urlparse(resp.url).hostname or "").lower() in ("circle.so", "www.circle.so")


@dataclass
class JoinClassification:
    join_type: str
    detail: str = ""
    #: The community's display name when the call answered 200 -- the same
    #: payload carries it, so the harvest doesn't ask a second time.
    name: str | None = None


def payload_name(data: dict) -> str | None:
    for key in ("name", "community_name", "title"):
        val = data.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()[:120]
    return None


def _price_label_fallback(price_label: str | None) -> JoinClassification | None:
    """Fallback signal for when the live communities/current check comes back
    UNKNOWN or LOCKED_UNKNOWN -- Circle's own Discover listing already states
    a price for any community sourced from a listing crawl (circle_directory,
    web-search finds), captured at discovery time into ``Community.price_label``
    and never blocked by the WAFs/apex-www quirks that break the live check on
    custom domains (confirmed by hand on 6 of these: the API said 404/401 while
    the real page had a working Join/Login button).

    Stays a fallback, not the primary signal: a "From $X" label only proves the
    cheapest tier costs money, it doesn't rule out a free tier existing too
    (``_classify_payload`` above already encodes that a paywall and open
    signups can coexist).
    """
    if not price_label or not price_label.strip():
        return None
    label = price_label.strip()
    if "free" in label.lower() or label.lower() in ("$0", "from $0"):
        return JoinClassification(JoinType.FREE_JOIN, f"price_label fallback: '{label}'")
    return JoinClassification(JoinType.PAID, f"price_label fallback: '{label}'")


def _classify_payload(data: dict) -> JoinClassification:
    """Pure classification from an already-fetched communities/current payload.

    Order matters: a free tier being joinable outranks a paywall also being
    present -- ``being-freelance``, ``talentcollective`` and others sell paid
    tiers but still let anyone sign up for the free one, which is what the
    test account can use.

    ``subscription_cancelled`` overrides everything else: the operator's own
    Circle plan lapsed, so ``allow_signups_to_public_community`` can still
    read ``true`` from before the lapse while the live site actually serves
    every visitor a "Circle plan has expired" page (``trigify-social-circle``:
    flagged free_join by this payload, confirmed dead by hand).
    """
    if data.get("subscription_cancelled"):
        return JoinClassification(JoinType.SUBSCRIPTION_EXPIRED, "subscription_cancelled=true")
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


def _fetch_join_classification_once(
    host: str, *, http: requests.Session, timeout: int
) -> JoinClassification:
    try:
        resp = http.get(
            f"https://{host}/internal_api/communities/current",
            headers={"User-Agent": BROWSER_UA, "Accept": "application/json"},
            timeout=timeout,
        )
    except requests.RequestException as exc:
        return JoinClassification(JoinType.UNKNOWN, f"request failed: {exc.__class__.__name__}")

    if resp.status_code in (401, 403):
        if _redirects_to_marketing_site(host, session=http, timeout=timeout):
            return JoinClassification(
                JoinType.UNKNOWN,
                "host no longer maps to a community (redirects to circle.so marketing site)",
            )
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

    classification = _classify_payload(data)
    classification.name = payload_name(data)
    return classification


def fetch_join_classification(
    host: str, *, session: requests.Session | None = None, timeout: int = 12
) -> JoinClassification:
    """Classify one host's join requirement. Never raises.

    A 401/403 on this metadata call is its own outcome (``locked_unknown``):
    the community is locked down even further than a private space list, and
    free vs. paid genuinely can't be told apart without a human on the join
    page.

    Retries once against ``www.<host>`` when the apex host comes back
    UNKNOWN (never LOCKED_UNKNOWN -- a 401/403 already means something real
    is there, just refusing us). Confirmed by hand on 3 BuiltWith custom
    domains that this isn't a hypothetical: bravelybeingyou.com and
    grocommunity.se both 404 on the bare apex for this exact path with no
    redirect (though their homepage *does* redirect to www), and icenet.work
    hits a redirect loop on the apex that simply isn't there on www -- all
    three return a normal 200 + valid payload on the www host. Requests
    already follows an HTTP redirect when the host issues one (e.g.
    creativeleader.net's apex 301s to www before its 401), so this only
    covers the cases where no such redirect exists for this specific path.
    """
    http = session or shared_session()
    host = (host or "").replace("https://", "").replace("http://", "").strip("/")
    if not host:
        return JoinClassification(JoinType.UNKNOWN, "empty host")

    classification = _fetch_join_classification_once(host, http=http, timeout=timeout)
    if classification.join_type == JoinType.UNKNOWN and not host.lower().startswith("www."):
        retry = _fetch_join_classification_once(f"www.{host}", http=http, timeout=timeout)
        if retry.join_type != JoinType.UNKNOWN:
            return JoinClassification(
                retry.join_type,
                f"{retry.detail} (www retry; apex failed: {classification.detail})",
                name=retry.name,
            )
    return classification


def classify_join_type_pending(
    db, *, limit: int | None = None, recheck: bool = False
) -> dict[str, int]:
    """Backfill join_type for communities that never got a live check.

    Mirrors circle_leads/pipeline.py::classify_icp_pending's shape (only rows
    with join_type_checked_at IS NULL, ordered by id, optional limit) -- but
    this classifier itself needs no browser, just one HTTP GET per host
    (fetch_join_classification, above), so it's cheap enough to run over the
    whole backlog in one pass rather than only the ICP-flagged subset.
    ``recheck`` re-classifies every community instead of only never-checked
    ones -- e.g. to backfill join_type_detail (P21) onto rows classified
    before that column existed, or after a classification-rule change.
    Runs at low priority: it shares Circle's per-IP budget with reads that
    can produce leads, and yields to them.
    """
    stats: dict[str, int] = {"checked": 0}
    session = shared_session()
    with priority(LOW):
        _classify_rows(db, session, stats, limit=limit, recheck=recheck)
    return stats


def _classify_rows(db, session, stats, *, limit, recheck) -> None:
    from sqlalchemy import select

    from circle_leads.storage.models import Community, utcnow

    with db.session() as s:
        query = select(Community.id).order_by(Community.id)
        if not recheck:
            query = query.where(Community.join_type_checked_at.is_(None))
        if limit:
            query = query.limit(limit)
        pending_ids = list(s.scalars(query).all())

    for community_pk in pending_ids:
        with db.session() as s:
            community = s.get(Community, community_pk)
            if community is None:
                continue
            host = urlparse(community.url).hostname or (
                community.url.replace("https://", "").replace("http://", "").strip("/")
            )
            classification = fetch_join_classification(host, session=session)
            if classification.join_type in (JoinType.UNKNOWN, JoinType.LOCKED_UNKNOWN):
                fallback = _price_label_fallback(community.price_label)
                if fallback is not None:
                    classification = JoinClassification(
                        fallback.join_type,
                        f"{fallback.detail} (live check inconclusive: {classification.detail})",
                    )
            community.join_type = classification.join_type
            community.join_type_detail = classification.detail[:2000]
            community.join_type_checked_at = utcnow()
            stats["checked"] += 1
            stats[classification.join_type] = stats.get(classification.join_type, 0) + 1
