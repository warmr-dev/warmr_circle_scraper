"""Shared, pooled HTTP session for polite, efficient repeated reads.

One process-wide session with:
- Connection pooling + HTTP keep-alive, so repeated requests to the same host
  reuse the TCP + TLS handshake instead of paying for a new one each time.
- Automatic retry with exponential backoff on transient failures (429/5xx).
- A browser User-Agent, which Circle's public JSON API expects.

Reusing one session across every community reader is the "smart handshake":
the connection to a host we already talked to is kept warm in the pool, so
re-reading it later does not open a fresh connection.
"""

from __future__ import annotations

import threading

import requests
from requests.adapters import HTTPAdapter

try:  # urllib3 ships with requests; import defensively across versions.
    from urllib3.util.retry import Retry
except Exception:  # pragma: no cover
    Retry = None  # type: ignore

BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

_lock = threading.Lock()
_session: requests.Session | None = None


def _build_session(pool_size: int = 20) -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": BROWSER_UA})
    retry = None
    if Retry is not None:
        retry = Retry(
            total=3,
            connect=3,
            read=2,
            backoff_factor=0.6,  # 0s, 0.6s, 1.2s, ...
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset({"GET", "HEAD"}),
            respect_retry_after_header=True,
            raise_on_status=False,
        )
    adapter = HTTPAdapter(
        pool_connections=pool_size,   # distinct hosts kept in the pool
        pool_maxsize=pool_size,       # connections kept per host
        max_retries=retry,
        pool_block=False,
    )
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s


def shared_session() -> requests.Session:
    """Return the process-wide pooled session, building it once."""
    global _session
    if _session is None:
        with _lock:
            if _session is None:
                _session = _build_session()
    return _session
