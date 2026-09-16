"""Register the communities we monitor with the Warmr portal.

Parallel to ``vini_ingest`` (which does the same for leads). Every community we
actively watch -- a subscribed or operator-approved public Circle community, or
a private one connected via a stored member session -- is POSTed to the portal's
community intake endpoint so the partner-research view holds the full set.

Configured via env (optional -- unset means the sync is a no-op):

    COMMUNITY_INTAKE_API_SECRET   shared secret for the ``x-community-intake-secret`` header
    COMMUNITY_INTAKE_URL          override endpoint (defaults to the portal route)

The endpoint accepts either a single community object or an array; this module
sends an array (chunked) so a whole backfill is a few requests, not hundreds.
Only communities whose payload changed since the last successful push are sent;
the per-community fingerprint cache lives in the DB ``settings`` table, so a
steady-state run POSTs nothing.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any

import requests
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from circle_leads.discovery.discover_communities import community_slug_for_host
from circle_leads.discovery.validate_finds import is_subdomain_community
from circle_leads.storage.database import Database
from circle_leads.storage.models import (
    CircleConnection,
    Community,
    ConnectionPriority,
    PermissionStatus,
    ReplaySession,
)

logger = logging.getLogger(__name__)

DEFAULT_INTAKE_URL = "https://portal.warmrhq.com/api/external/communities"
# Identifies this feed on the portal side (mirrors vini_ingest's PARSER_NAME).
SOURCE = "circle_scraper"
# Sync-state cache: community key -> payload fingerprint. Stored in the DB
# settings table so a run that changes nothing sends nothing.
SYNC_STATE_KEY = "community_intake_state"
# Max communities per POST body. A 50-item first backfill batch read-timed-out
# against the portal (30s) -- looked like a cold serverless start under a big
# synchronous batch write; 10-item batches and a resend both went through
# fine. Kept small so one slow batch can't stall a whole harvest.
CHUNK = 10


@dataclass
class CommunityIntakeConfig:
    url: str
    api_secret: str

    @property
    def enabled(self) -> bool:
        return bool(self.url and self.api_secret)


@dataclass
class IntakeResult:
    attempted: int = 0
    sent: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)


@dataclass
class MonitoredCommunity:
    key: str                 # stable dedup key: "community:<slug>" / "connection:<host>"
    payload: dict[str, Any]


def load_community_intake_config() -> CommunityIntakeConfig:
    return CommunityIntakeConfig(
        url=(
            os.environ.get("COMMUNITY_INTAKE_URL") or DEFAULT_INTAKE_URL
        ).strip(),
        api_secret=(os.environ.get("COMMUNITY_INTAKE_API_SECRET") or "").strip(),
    )


def parse_entry_cost(price_label: str | None) -> int | None:
    """Best-effort whole-number price from a free-text label.

    "$49/mo" -> 49 ; "$1,200/yr" -> 1200 ; "USD 499" -> 499 ; "Free" -> 0 ;
    "" / "members only" / None -> None (unknown, so the field is omitted).
    """
    if not price_label:
        return None
    low = price_label.strip().lower()
    if not low:
        return None
    if "free" in low:
        return 0
    m = re.search(r"\d[\d,]*(?:\.\d+)?", low)
    if not m:
        return None
    try:
        return int(round(float(m.group(0).replace(",", ""))))
    except ValueError:
        return None


def _is_paid_membership(price_label: str | None, entry_cost: int | None) -> bool | None:
    """True/False when we can tell, None when the price is unknown."""
    if entry_cost is not None:
        return entry_cost > 0
    if price_label and "free" in price_label.lower():
        return False
    return None


def _reads_on_circle(platform: str | None, url: str) -> bool:
    """Mirror of ``harvest._reads_on_circle``: a community the readers can read.

    Any Circle-hosted community (a ``<slug>.circle.so`` subdomain or a custom
    domain classified as ``circle``). Rows created before the ``platform``
    column carry NULL and fall back to the URL shape.
    """
    if platform is not None:
        return platform == "circle"
    return is_subdomain_community(url)


def _norm_host(url_or_host: str) -> str:
    h = (url_or_host or "").strip().lower()
    h = h.replace("https://", "").replace("http://", "").strip("/")
    return h.split("/")[0]


def collect_monitored_communities(session: Session) -> list[MonitoredCommunity]:
    """The communities we actively monitor, as portal-intake payloads.

    The set: public Circle communities that are watched or operator-approved,
    plus private communities that have a stored member session and aren't
    paused. A private connection that shares a host with a public row is folded
    into the public row (the connection is dropped as a duplicate).
    """
    out: list[MonitoredCommunity] = []
    seen_hosts: set[str] = set()

    # 1. Public communities we deliberately monitor: watched, or approved.
    stmt = (
        select(Community)
        .where(
            or_(
                Community.watching.is_(True),
                Community.permission_status == PermissionStatus.APPROVED.value,
            )
        )
        .order_by(Community.slug)
    )
    for c in session.scalars(stmt).all():
        if not _reads_on_circle(c.platform, c.url):
            continue  # Discover listings / FB / Slack / dead hosts aren't monitored
        name = (c.name or c.slug or "").strip()
        if not name or not c.url:
            continue
        entry_cost = parse_entry_cost(c.price_label)
        payload: dict[str, Any] = {
            "name": name,
            "slug": c.slug,
            "platform": "circle",
            "join_link": c.url,
            "is_active": True,
            "source": SOURCE,
        }
        if entry_cost is not None:
            payload["entry_cost"] = entry_cost
        paid = _is_paid_membership(c.price_label, entry_cost)
        if paid is not None:
            payload["is_paid_membership"] = paid
        if (c.notes or "").strip():
            payload["notes"] = c.notes.strip()
        out.append(MonitoredCommunity(key=f"community:{c.slug}", payload=payload))
        seen_hosts.add(_norm_host(c.url))

    # 2. Private communities connected with a stored member session (not paused).
    with_session = {r.host for r in session.scalars(select(ReplaySession)).all()}
    conn_stmt = (
        select(CircleConnection)
        .where(CircleConnection.priority != ConnectionPriority.PAUSED.value)
        .order_by(CircleConnection.host)
    )
    for conn in session.scalars(conn_stmt).all():
        host = _norm_host(conn.host)
        if host not in with_session or host in seen_hosts:
            continue
        payload = {
            "name": (conn.name or host).strip(),
            "slug": community_slug_for_host(host),
            "platform": "circle",
            "join_link": f"https://{host}",
            "visibility": "private",
            "is_active": True,
            "source": SOURCE,
        }
        if (conn.notes or "").strip():
            payload["notes"] = conn.notes.strip()
        out.append(MonitoredCommunity(key=f"connection:{host}", payload=payload))
        seen_hosts.add(host)

    return out


def _fingerprint(payload: dict[str, Any]) -> str:
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _load_state(db: Database) -> dict[str, str]:
    from circle_leads.storage.settings_store import get_setting

    raw = get_setting(db, SYNC_STATE_KEY)
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except (ValueError, TypeError):
        return {}


def _save_state(db: Database, state: dict[str, str]) -> None:
    from circle_leads.storage.settings_store import set_setting

    set_setting(db, SYNC_STATE_KEY, json.dumps(state, sort_keys=True))


def post_communities(
    items: list[dict[str, Any]],
    *,
    config: CommunityIntakeConfig | None = None,
    session: requests.Session | None = None,
    timeout: float = 60.0,
) -> None:
    """POST a non-empty community array. Raises on HTTP / network failure."""
    if not items:
        return
    cfg = config or load_community_intake_config()
    if not cfg.enabled:
        raise RuntimeError(
            "Community intake is not configured (need COMMUNITY_INTAKE_API_SECRET)."
        )
    headers = {
        "Content-Type": "application/json",
        "x-community-intake-secret": cfg.api_secret,
    }
    http = session or requests.Session()
    response = http.post(cfg.url, json=items, headers=headers, timeout=timeout)
    if response.status_code >= 400:
        body = (response.text or "")[:500]
        raise RuntimeError(
            f"Community intake HTTP {response.status_code}: {body or response.reason}"
        )


def push_monitored_communities(
    db: Database,
    *,
    config: CommunityIntakeConfig | None = None,
    force: bool = False,
    session: requests.Session | None = None,
) -> IntakeResult:
    """Register every monitored community with the Warmr portal.

    Sends only communities whose payload changed since the last successful push
    (``force`` re-sends all). A no-op when the endpoint isn't configured.
    """
    result = IntakeResult()
    cfg = config or load_community_intake_config()
    if not cfg.enabled:
        return result

    with db.session() as s:
        monitored = collect_monitored_communities(s)
    if not monitored:
        return result

    state = _load_state(db)
    fingerprints: dict[str, str] = {}
    pending: list[dict[str, Any]] = []
    pending_keys: list[str] = []

    for mc in monitored:
        fp = _fingerprint(mc.payload)
        fingerprints[mc.key] = fp
        if not force and state.get(mc.key) == fp:
            result.skipped += 1
            continue
        pending.append(mc.payload)
        pending_keys.append(mc.key)

    result.attempted = len(pending)

    http = session or requests.Session()
    new_state = dict(state)
    for i in range(0, len(pending), CHUNK):
        batch = pending[i:i + CHUNK]
        batch_keys = pending_keys[i:i + CHUNK]
        try:
            post_communities(batch, config=cfg, session=http)
        except Exception as exc:  # noqa: BLE001 - report per batch, keep going
            result.errors.append(str(exc))
            logger.exception(
                "Failed to push %d community/ies to the portal", len(batch)
            )
            continue
        for k in batch_keys:
            new_state[k] = fingerprints[k]
        result.sent += len(batch)

    # Forget communities that left the monitored set so the cache can't grow
    # forever and a re-added one re-syncs. (The portal is not told they left --
    # deactivation would need an explicit tombstone; out of scope here.)
    new_state = {k: v for k, v in new_state.items() if k in fingerprints}
    if new_state != state:
        _save_state(db, new_state)
    return result
