"""Bulk crawl of Circle's own public discovery marketplace (discover.circle.so).

This is a *floor*, not the whole platform: discover.circle.so lists communities
whose owner opted into Circle's marketplace, confirmed at ~2,095 total (see
``pagination.total_count`` on an unfiltered listings call) -- a small slice of
the ~20-30k communities Circle's own trend reports count platform-wide. Most of
the platform lives outside this directory (custom domains never listed here,
private communities); that gap is why the web-search discovery path
(discovery/web_search.py) and a future DNS/CNAME source stay load-bearing rather
than becoming redundant once this module exists.

Requires a real browser, not requests/urllib: a plain HTTP GET to
discover.circle.so gets a Cloudflare "Verifying you are a human" JS challenge on
every attempt (confirmed with curl -- ``cf-mitigated: challenge`` on the response).
A stock headless Chromium with no stealth flags and no fingerprint spoofing loads
it normally on the first try, so a real browser is the correct tool here, not an
escalation -- the same posture ``remote_browser/session.py`` already uses
elsewhere in this codebase.

API shape (captured from the site's own frontend network calls, not guessed):
  GET /discover/public_api/goals?featured=true&per_page=20
      -> {"records": [{id, name, slug, long_description, position,
                        homepage_featured}, ...]}
  GET /discover/public_api/listings/search?goal_slugs[]=<slug>&tags=&page=<n>&per_page=100
      -> {"records": [{id, name, short_description, slug, human_readable_price,
                        price_in_cents, cover_image_url, video}, ...],
          "pagination": {total_count, page, per_page}}
      (100 is the server-enforced per_page ceiling; omitting goal_slugs returns
      the flat full directory in one pass, but per-goal calls are still needed to
      learn which goal(s) each listing appears under.)

A listing record carries no real community host -- ``slug`` is a
discover.circle.so/products/<slug> path, not the community's own subdomain.
Resolving the actual join URL needs one more visit per listing: the "Join" (or
"Sign up" / "Get access") anchor's href on that product page, which is sometimes
the real <slug2>.circle.so and sometimes an external marketing funnel (confirmed
on a real listing). This module does not try to resolve funnels further -- it
hands whatever URL it finds to the existing platform-detection pipeline
(``discovery.discover_communities.detect_platform`` via ``discovery.persist``),
which already knows how to tell a real Circle host from an external one.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

DISCOVER_BASE = "https://discover.circle.so"
GOALS_ENDPOINT = f"{DISCOVER_BASE}/discover/public_api/goals?featured=true&per_page=20"
LISTINGS_ENDPOINT = f"{DISCOVER_BASE}/discover/public_api/listings/search"
LISTINGS_PER_PAGE = 100  # server-enforced ceiling; requesting more silently clamps

BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

# Anchor text that indicates "this is how you get into the community", checked
# in this order. Different listings phrase it differently (free vs. paid,
# waitlist vs. instant). "buy" added after live testing (circle_leads/join/)
# found a paid listing's only CTA is "Buy" -- the exact same gap left this
# listing (and others like it) stuck at platform="discover" (unresolved)
# instead of a real host, since the old list never matched its anchor at all.
_JOIN_ANCHOR_TEXTS = ("join", "sign up", "get access", "get started", "enroll", "buy")


@dataclass
class DirectoryGoal:
    id: int
    slug: str
    name: str


@dataclass
class DirectoryListing:
    external_id: int
    slug: str
    name: str | None
    description: str | None
    price_label: str | None
    price_in_cents: int | None
    goals: list[str] = field(default_factory=list)
    join_url: str | None = None  # resolved lazily; None until resolve_join_urls runs


@dataclass
class DirectoryCrawlResult:
    goals: list[DirectoryGoal] = field(default_factory=list)
    listings: list[DirectoryListing] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def _require_playwright():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover - depends on the image
        raise RuntimeError(
            "Playwright is not installed. discover-directory needs a real "
            "browser (Cloudflare challenges plain HTTP requests to "
            "discover.circle.so). Install with:\n"
            "  pip install 'circle-leads[browser]'\n"
            "  playwright install --with-deps chromium"
        ) from exc
    return sync_playwright


def fetch_goals(page) -> list[DirectoryGoal]:
    """The real goal categories, fetched live (not hardcoded -- Circle adds and
    renames these; a stale hardcoded list would silently under-cover the
    directory)."""
    resp = page.request.get(GOALS_ENDPOINT, headers={"Accept": "application/json"})
    data = resp.json()
    return [
        DirectoryGoal(id=r["id"], slug=r["slug"], name=r["name"])
        for r in data.get("records", [])
    ]


def _fetch_listings_page(page, *, goal_slug: str | None, page_num: int) -> tuple[list[dict], dict]:
    params = f"tags=&page={page_num}&per_page={LISTINGS_PER_PAGE}"
    if goal_slug:
        params = f"goal_slugs[]={goal_slug}&{params}"
    resp = page.request.get(f"{LISTINGS_ENDPOINT}?{params}", headers={"Accept": "application/json"})
    if resp.status != 200:
        return [], {}
    data = resp.json()
    return data.get("records", []), data.get("pagination", {})


def crawl_listings(
    page, goals: list[DirectoryGoal], *, request_pause: float = 0.15
) -> dict[int, DirectoryListing]:
    """Walk every goal's paginated listings, deduping by id.

    A listing legitimately appears under multiple goals -- this records every
    goal it was seen under rather than keeping only the first.
    """
    by_id: dict[int, DirectoryListing] = {}
    for goal in goals:
        page_num = 1
        while True:
            records, pagination = _fetch_listings_page(page, goal_slug=goal.slug, page_num=page_num)
            if not records:
                break
            for r in records:
                rid = r["id"]
                existing = by_id.get(rid)
                if existing is None:
                    by_id[rid] = DirectoryListing(
                        external_id=rid,
                        slug=r.get("slug"),
                        name=r.get("name"),
                        description=r.get("short_description"),
                        price_label=r.get("human_readable_price"),
                        price_in_cents=r.get("price_in_cents"),
                        goals=[goal.slug],
                    )
                elif goal.slug not in existing.goals:
                    existing.goals.append(goal.slug)
            total = pagination.get("total_count", 0)
            per_page = pagination.get("per_page", LISTINGS_PER_PAGE)
            if page_num * per_page >= total:
                break
            page_num += 1
            time.sleep(request_pause)
    return by_id


def resolve_join_url(page, slug: str, *, timeout: int = 20_000) -> str | None:
    """Find where a listing's product page actually sends a visitor to join.

    Requires a real page render (`page.goto`), not a lightweight request: the
    "Join" control's href isn't reliably regex-matchable in raw HTML across
    listings (some render it through nested elements). The result is sometimes
    the real `<slug>.circle.so`, sometimes an external marketing funnel -- both
    are handed as-is to the existing platform-detection pipeline, which resolves
    which is which; this function doesn't try to follow funnels further.
    """
    url = f"{DISCOVER_BASE}/products/{slug}"
    try:
        page.goto(url, wait_until="load", timeout=timeout)
        page.wait_for_timeout(2000)  # let client-side hydration finish
    except Exception as exc:  # noqa: BLE001 - a dead/slow listing is not fatal
        logger.info("Could not load product page for %s: %s", slug, exc.__class__.__name__)
        return None

    for text in _JOIN_ANCHOR_TEXTS:
        try:
            locator = page.locator(f"a:has-text('{text}')")
            # A page can render more than one matching anchor (e.g. a hidden
            # duplicate for a responsive layout) -- take the first VISIBLE one,
            # not just the first in DOM order.
            for i in range(locator.count()):
                candidate = locator.nth(i)
                if candidate.is_visible():
                    href = candidate.get_attribute("href")
                    if href:
                        return href
        except Exception:  # noqa: BLE001 - keep trying the next candidate text
            continue
    return None


def recheck_unresolved_join_urls(db, *, limit: int | None = None,
                                 request_pause: float = 0.15) -> dict:
    """Re-run resolve_join_url() for rows still stuck at platform='discover'
    (the join-anchor lookup failed at crawl time -- often because
    _JOIN_ANCHOR_TEXTS was missing a text the listing actually used, e.g.
    "buy" for a paid listing before it was added here). Cheap compared to a
    full crawl: one browser context, one visit per stuck row, no goal/listing
    re-fetch. Updates url/platform/join_type in place when a real host is
    now found.
    """
    from urllib.parse import urlparse

    from sqlalchemy import select

    from circle_leads.discovery.discover_communities import (
        PLATFORM_CIRCLE, PLATFORM_DISCOVER, detect_platform,
    )
    from circle_leads.discovery.join_type import (
        fetch_join_classification,
        with_price_label_fallback,
    )
    from circle_leads.storage.models import Community, utcnow

    sync_playwright = _require_playwright()
    with db.session() as s:
        query = select(Community.id, Community.url).where(Community.platform == PLATFORM_DISCOVER)
        if limit:
            query = query.limit(limit)
        targets = s.execute(query).all()

    resolved = still_unresolved = errors = 0
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=["--disable-dev-shm-usage"])
        try:
            page = browser.new_page(user_agent=BROWSER_UA)
            for community_id, old_url in targets:
                # A still-unresolved row's url is still the discover.circle.so
                # /products/<slug> link (never rewritten), so that path's last
                # segment is the listing slug resolve_join_url() needs.
                path_slug = urlparse(old_url).path.rstrip("/").rsplit("/", 1)[-1]
                try:
                    href = resolve_join_url(page, path_slug)
                except Exception as exc:  # noqa: BLE001 - one bad listing must not kill the pass
                    logger.info("recheck failed for %s: %s", path_slug, exc.__class__.__name__)
                    errors += 1
                    continue
                if not href:
                    still_unresolved += 1
                    continue

                new_platform = detect_platform(href)
                classification = fetch_join_classification(href) if new_platform == PLATFORM_CIRCLE else None
                try:
                    with db.session() as s:
                        c = s.get(Community, community_id)
                        if c is None:
                            continue
                        c.url = href
                        c.platform = new_platform
                        if classification is not None:
                            classification = with_price_label_fallback(classification, c.price_label)
                            c.join_type = classification.join_type
                            c.join_type_detail = classification.detail[:2000]
                            c.join_type_checked_at = utcnow()
                    resolved += 1
                except Exception as exc:  # noqa: BLE001 - e.g. a unique-url clash with an existing row
                    logger.info("could not save resolved url for %s: %s", path_slug, exc.__class__.__name__)
                    errors += 1
                time.sleep(request_pause)
        finally:
            browser.close()

    return {"total": len(targets), "resolved": resolved,
            "still_unresolved": still_unresolved, "errors": errors}


def crawl_directory(*, request_pause: float = 0.15, resolve_join_urls: bool = True) -> DirectoryCrawlResult:
    """Full crawl: goals -> every goal's listings -> (optionally) each listing's
    real join URL.

    One browser context for the whole run: the Cloudflare JS challenge is paid
    once on the first navigation, and the cleared session's cookies carry
    forward for every subsequent request in this context.
    """
    sync_playwright = _require_playwright()
    result = DirectoryCrawlResult()

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=["--disable-dev-shm-usage"])
        try:
            page = browser.new_page(user_agent=BROWSER_UA)
            page.goto(DISCOVER_BASE + "/", wait_until="load", timeout=45_000)

            result.goals = fetch_goals(page)
            if not result.goals:
                result.errors.append("No goal categories returned; directory may be unreachable.")
                return result

            by_id = crawl_listings(page, result.goals, request_pause=request_pause)
            result.listings = list(by_id.values())

            if resolve_join_urls:
                for listing in result.listings:
                    listing.join_url = resolve_join_url(page, listing.slug)
                    time.sleep(request_pause)
        finally:
            browser.close()

    return result


def _to_ranked(listing: DirectoryListing):
    """One DirectoryListing -> one RankedCommunity, sharing the same scoring
    heuristic as web-search finds (finder.score_listing) so both sources land
    on a comparable relevance_score.

    Deliberately does not reuse ``finder.rank_discover_listings``'s slug
    derivation (``path.split('/')[-1]``): that assumes a Discover ``/products/
    <slug>`` path, but a resolved join URL here is often the community's own
    host or an external funnel, so the slug comes from
    ``unique_slug_for_host`` on whatever host was actually resolved -- the
    unique variant, because a slug is a key here and the label variant collapses
    sibling subdomains of one domain onto a single row.
    """
    from circle_leads.discovery.discover_communities import unique_slug_for_host
    from circle_leads.discovery.finder import RankedCommunity, score_listing, is_free
    from urllib.parse import urlparse

    join_url = listing.join_url or f"{DISCOVER_BASE}/products/{listing.slug}"
    host = urlparse(join_url if "://" in join_url else f"https://{join_url}").hostname or listing.slug
    slug = unique_slug_for_host(host) if listing.join_url else (listing.slug or str(listing.external_id))

    score, reasons = score_listing(listing.name, listing.description, listing.price_label)
    return RankedCommunity(
        slug=slug,
        name=listing.name,
        join_url=join_url,
        score=score,
        price_label=listing.price_label,
        is_free=is_free(listing.price_label),
        reasons=reasons,
        source="circle_directory",
        description=listing.description,
    )


def persist_crawl_result(db, result: DirectoryCrawlResult, *, min_score: int = 0):
    """Persist a crawl into the communities table, then backfill the
    directory-only provenance fields persist_finds doesn't know about.

    Reuses discovery.persist.persist_finds for the actual upsert (same
    dedup/scoring path as web-search finds) rather than a parallel write path;
    validate=False since detect_platform/validate_url per listing is redundant
    work the join-bot's own pre-check (join_type.fetch_join_classification)
    would redo anyway -- let ICP-flagged communities pay for that, not all 2k+.
    """
    from circle_leads.discovery.persist import persist_finds
    from circle_leads.storage.models import Community, utcnow
    from sqlalchemy import select

    by_slug = {}
    ranked = []
    for listing in result.listings:
        rc = _to_ranked(listing)
        by_slug[rc.slug] = listing
        ranked.append(rc)

    persisted = persist_finds(db, ranked, source="circle_directory", min_score=min_score, validate=False)

    now = utcnow()
    with db.session() as s:
        for rc in ranked:
            listing = by_slug.get(rc.slug)
            if listing is None:
                continue
            community = s.scalar(select(Community).where(Community.slug == rc.slug))
            if community is None:
                continue
            community.external_directory_id = str(listing.external_id)
            existing_goals = set(community.directory_goals or [])
            community.directory_goals = sorted(existing_goals | set(listing.goals))
            community.directory_synced_at = now

    return persisted

