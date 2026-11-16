"""Give a Circle Discover card the address of the community it sells.

Search and the directory crawl store a listing as
``https://discover.circle.so/products/<slug>`` with no ``communities.host``.
The join-type check and the join queue need the community's own address
(``reach.real_host``), so a card never reached either: on 2026-11-16 there
were 1647 such rows, among them a free community anyone can join
(Marketing Talent Exchange, found 2026-11-12).

The listing page says where its Join button goes (``listing_from_html``). When
that is a Circle community, the row takes its host and the scheduled join-type
check asks Circle about it on its next pass. A card whose button leaves Circle
(an outside sales page), whose page shows no link, or that Circle took down
is noted and not fetched again. A card for a community another row already
holds is noted as that row's duplicate.

One plain GET per card -- no browser. The worker runs a bounded batch with the
join-type check (``cli._run_community_maintenance``).
"""

from __future__ import annotations

import logging
import time
from datetime import datetime

import requests
from sqlalchemy import and_, nullslast, or_, select

from circle_leads.discovery.discover_communities import (
    PLATFORM_CIRCLE,
    PLATFORM_OTHER,
    detect_platform,
)
from circle_leads.discovery.validate_finds import BROWSER_UA, listing_from_html
from circle_leads.scraper.governor import LOW, priority
from circle_leads.scraper.http_client import shared_session
from circle_leads.storage.database import Database, community_host
from circle_leads.storage.models import Community, utcnow

logger = logging.getLogger(__name__)

CARD_URL_PREFIX = "https://discover.circle.so/products/"
# Written into ``notes`` once a card is settled without an address; such a row
# is not fetched again.
NOTE_PREFIX = "directory card:"
# Cards per scheduled pass: about a minute of requests, and the 1647 cards of
# 2026-11-16 are through in a day and a half at the hourly join-type schedule.
SCHEDULED_BATCH = 50


def pending_cards():
    """Cards with no address that nothing has settled yet."""
    return and_(
        Community.host.is_(None),
        Community.url.startswith(CARD_URL_PREFIX),
        or_(Community.notes.is_(None), ~Community.notes.startswith(NOTE_PREFIX)),
    )


def _note(existing: str | None, line: str) -> str:
    return f"{line}\n{existing}" if existing else line


def apply_card_link(s, community: Community, link: str, platform: str,
                    *, now: datetime | None = None) -> str:
    """Point a card's row at the community its Join button leads to.

    Returns what happened: ``resolved``, ``duplicate`` or ``not_circle``.
    """
    now = now or utcnow()
    card_url = community.url
    host = community_host(link)
    if platform != PLATFORM_CIRCLE or not host:
        community.platform = PLATFORM_OTHER
        community.notes = _note(community.notes,
                                f"{NOTE_PREFIX} its Join button leads off Circle: {link[:300]}")
        return "not_circle"
    holder = s.scalar(select(Community.id).where(Community.host == host,
                                                  Community.id != community.id))
    if holder is not None:
        community.notes = _note(community.notes,
                                f"{NOTE_PREFIX} same community as #{holder} ({host})")
        return "duplicate"
    root = f"https://{host}"
    url_taken = s.scalar(select(Community.id).where(Community.url == root,
                                                     Community.id != community.id))
    community.host = host
    community.url = link if url_taken is not None else root
    community.platform = PLATFORM_CIRCLE
    # Ask Circle on the next join-type pass; the card's price only decides
    # when Circle's own answer is inconclusive (join_type.refine_join_classification).
    community.join_type_checked_at = None
    community.notes = _note(community.notes, f"{NOTE_PREFIX} found as {card_url}")
    community.updated_at = now
    return "resolved"


def resolve_directory_cards(db: Database, *, limit: int | None = SCHEDULED_BATCH,
                            session: requests.Session | None = None,
                            pause: float = 1.0) -> dict[str, int]:
    """Read up to ``limit`` unsettled cards and store what each one leads to.

    ICP-fit cards first, then the ones looked at longest ago. Network work
    happens outside any database session.
    """
    stats = {"checked": 0, "resolved": 0, "duplicate": 0, "not_circle": 0,
             "no_link": 0, "gone": 0, "errors": 0}
    http = session or shared_session()
    with db.session() as s:
        query = (select(Community.id, Community.url).where(pending_cards())
                 .order_by(nullslast(Community.icp_flag.desc()),
                           Community.updated_at.asc(), Community.id))
        if limit:
            query = query.limit(limit)
        cards = s.execute(query).all()

    with priority(LOW):
        for i, (community_id, card_url) in enumerate(cards):
            if i and pause:
                time.sleep(pause)
            stats["checked"] += 1
            try:
                resp = http.get(card_url, headers={"User-Agent": BROWSER_UA},
                                timeout=15, allow_redirects=True)
            except requests.RequestException as exc:
                logger.info("directory card %s: %s", card_url, exc.__class__.__name__)
                outcome = "errors"
            else:
                outcome = _settle(db, community_id, card_url, resp, http)
            stats[outcome] += 1
            if outcome == "errors":
                # To the back of the line, so one bad card cannot hold the batch.
                with db.session() as s:
                    row = s.get(Community, community_id)
                    if row is not None:
                        row.updated_at = utcnow()
    return stats


def _settle(db: Database, community_id: int, card_url: str,
            resp: requests.Response, http) -> str:
    if resp.status_code == 404:
        with db.session() as s:
            row = s.get(Community, community_id)
            if row is not None:
                row.notes = _note(row.notes, f"{NOTE_PREFIX} listing gone (HTTP 404) "
                                             f"{utcnow():%Y-%m-%d}")
        return "gone"
    if resp.status_code >= 400:
        return "errors"
    listing = listing_from_html(resp.text or "")
    platform = detect_platform(listing.join_link, session=http) if listing.join_link else None
    with db.session() as s:
        row = s.get(Community, community_id)
        if row is None or row.host is not None or row.url != card_url:
            return "errors"   # changed under us; the next pass sees the new state
        if listing.price_label:
            row.price_label = listing.price_label
        if not listing.join_link:
            row.notes = _note(row.notes, f"{NOTE_PREFIX} the listing shows no Join link")
            return "no_link"
        return apply_card_link(s, row, listing.join_link, platform)
