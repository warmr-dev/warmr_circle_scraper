"""Shared, pooled HTTP session for polite, efficient repeated reads.

One process-wide session with:
- Connection pooling + HTTP keep-alive, so repeated requests to the same host
  reuse the TCP + TLS handshake instead of paying for a new one each time.
- Every request to a Circle host waits for the machine-wide budget in
  ``governor.py`` -- shared with other processes and the desktop app, because
  Circle rate-limits per IP, not per process.
- At most one retry, and never on 429: a retry is a real request that skips
  the budget, and retrying a rate limit only extends it. A dead host (TLS
  handshake failure, DNS) is not retried at all.
- A browser User-Agent, which Circle's public JSON API expects.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter

from circle_leads.scraper import governor as gov

try:  # urllib3 ships with requests; import defensively across versions.
    from urllib3.util.retry import Retry
except Exception:  # pragma: no cover
    Retry = None  # type: ignore

logger = logging.getLogger(__name__)

BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

#: A community whose public API reliably answers 200. A 429/challenge on one
#: host can be that host's own setting (or, on Railway, a replayed cookie);
#: only when this canary is refused too is our IP being throttled.
DEFAULT_CANARY = "https://ulule.circle.so/internal_api/communities/current"
CANARY_TTL_S = 60.0

_lock = threading.Lock()
_session: requests.Session | None = None
_canary_lock = threading.Lock()
_canary_checked_at = 0.0
_canary_blocked = False


def _pushback(resp) -> bool:
    headers = getattr(resp, "headers", None) or {}
    return resp.status_code == 429 or str(headers.get("cf-mitigated", "")).lower() == "challenge"


def _ip_blocked(adapter: HTTPAdapter) -> bool:
    """Ask the canary (outside the budget, at most once a minute per process)."""
    global _canary_checked_at, _canary_blocked
    with _canary_lock:
        if time.monotonic() - _canary_checked_at < CANARY_TTL_S:
            return _canary_blocked
        url = os.environ.get("CIRCLE_GOVERNOR_CANARY") or DEFAULT_CANARY
        req = requests.Request(
            "GET", url, headers={"User-Agent": BROWSER_UA, "Accept": "application/json"}
        ).prepare()
        try:
            r = HTTPAdapter.send(adapter, req, timeout=15)
            _canary_blocked = _pushback(r) or r.status_code == 403
        except requests.RequestException:
            _canary_blocked = False  # our network is down, not Circle refusing us
        _canary_checked_at = time.monotonic()
        return _canary_blocked


class GovernedAdapter(HTTPAdapter):
    """HTTPAdapter that spends the shared Circle budget for Circle hosts."""

    def send(self, request, **kwargs):
        host = urlparse(request.url).hostname
        governed = gov.enabled() and gov.is_circle_host(host)
        if governed:
            gov.get_governor().acquire(max_wait_s=gov.max_wait_from_env())
        resp = super().send(request, **kwargs)
        if governed and _pushback(resp) and _ip_blocked(self):
            gov.get_governor().trip(f"HTTP {resp.status_code} on {host}")
        return resp


def _retry():
    if Retry is None:
        return None
    return Retry(
        total=1,
        connect=1,
        read=0,
        other=0,
        status=1,
        backoff_factor=1.0,
        status_forcelist=(502, 503, 504),
        allowed_methods=frozenset({"GET", "HEAD"}),
        respect_retry_after_header=False,
        raise_on_status=False,
    )


def mount_governed(session: requests.Session, pool_size: int = 10) -> requests.Session:
    """Route a session's traffic through the shared Circle budget."""
    adapter = GovernedAdapter(
        pool_connections=pool_size,   # distinct hosts kept in the pool
        pool_maxsize=pool_size,       # connections kept per host
        max_retries=_retry(),
        pool_block=False,
    )
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def governed_session() -> requests.Session:
    """A new session (own cookies) that still shares the Circle budget."""
    return mount_governed(requests.Session())


def _build_session(pool_size: int = 20) -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": BROWSER_UA})
    return mount_governed(s, pool_size)


def shared_session() -> requests.Session:
    """Return the process-wide pooled session, building it once."""
    global _session
    if _session is None:
        with _lock:
            if _session is None:
                _session = _build_session()
    return _session
