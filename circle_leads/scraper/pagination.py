"""Rate-limited HTTP client with backoff and pagination helpers.

Circle documents 2,000 requests per 5 minutes per IP and a monthly allowance
as low as 5,000 on Business plans, and counts 429s against that allowance.
This client is built to stay well below those ceilings rather than treat them
as throughput targets.
"""

from __future__ import annotations

import logging
import random
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterator

import requests

from circle_leads.authentication.browser_session import redact

logger = logging.getLogger(__name__)

# Circle's docs recommend waiting ~60s on a 429.
RATE_LIMIT_COOLDOWN_SECONDS = 60.0
RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class RateLimiter:
    """Token-bucket limiter, thread-safe, shared across a run."""

    def __init__(self, requests_per_minute: int = 60):
        self.min_interval = 60.0 / max(1, requests_per_minute)
        self._lock = threading.Lock()
        self._last = 0.0

    def acquire(self) -> None:
        with self._lock:
            now = time.monotonic()
            wait = self.min_interval - (now - self._last)
            if wait > 0:
                time.sleep(wait)
                now = time.monotonic()
            self._last = now


@dataclass
class QuotaTracker:
    """Counts requests so a run can stop before exhausting a monthly allowance."""

    budget: int | None = None
    used: int = 0

    def record(self) -> None:
        self.used += 1

    @property
    def exhausted(self) -> bool:
        return self.budget is not None and self.used >= self.budget


class ApiError(RuntimeError):
    def __init__(self, status: int, message: str):
        self.status = status
        super().__init__(f"HTTP {status}: {message}")


class AccessDeniedError(ApiError):
    """401/403 — the account is not authorized for this resource. Stop."""


class CircleClient:
    """Thin, retrying JSON client for Circle's official APIs."""

    def __init__(
        self,
        base_url: str,
        headers_provider: Callable[[], dict[str, str]],
        *,
        requests_per_minute: int = 60,
        max_retries: int = 5,
        backoff_base: float = 2.0,
        max_backoff: float = 60.0,
        timeout: int = 30,
        quota: QuotaTracker | None = None,
        session: requests.Session | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self._headers_provider = headers_provider
        self.limiter = RateLimiter(requests_per_minute)
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.max_backoff = max_backoff
        self.timeout = timeout
        self.quota = quota or QuotaTracker()
        self.session = session or requests.Session()

    def get(self, path: str, params: dict | None = None) -> dict[str, Any]:
        return self._request("GET", path, params=params)

    def _request(self, method: str, path: str, **kwargs) -> dict[str, Any]:
        if self.quota.exhausted:
            raise ApiError(0, "Local request budget exhausted; stopping.")

        url = self.base_url + path
        attempt = 0
        while True:
            self.limiter.acquire()
            headers = self._headers_provider()
            try:
                resp = self.session.request(
                    method, url, headers=headers, timeout=self.timeout, **kwargs
                )
            except requests.RequestException as exc:
                if attempt >= self.max_retries:
                    raise ApiError(0, f"Network error after retries: {exc}") from exc
                self._sleep_backoff(attempt)
                attempt += 1
                continue

            self.quota.record()

            if resp.status_code in (401, 403):
                # Never retry an authorization failure -- it is a stop condition,
                # and retrying burns the monthly allowance for nothing.
                raise AccessDeniedError(
                    resp.status_code,
                    f"Not authorized for {path}. Verify operator approval and token.",
                )

            if resp.status_code == 404:
                raise ApiError(404, f"Not found: {path}")

            if resp.status_code in RETRYABLE_STATUS:
                if attempt >= self.max_retries:
                    raise ApiError(resp.status_code, f"Gave up retrying {path}")
                if resp.status_code == 429:
                    delay = self._retry_after(resp) or RATE_LIMIT_COOLDOWN_SECONDS
                    logger.warning("Rate limited on %s; sleeping %.0fs", path, delay)
                    time.sleep(delay)
                else:
                    self._sleep_backoff(attempt)
                attempt += 1
                continue

            if resp.status_code >= 400:
                raise ApiError(resp.status_code, resp.text[:300])

            try:
                return resp.json()
            except ValueError as exc:
                raise ApiError(resp.status_code, "Response was not JSON") from exc

    @staticmethod
    def _retry_after(resp: requests.Response) -> float | None:
        # Not documented by Circle, but honored when present.
        raw = resp.headers.get("Retry-After")
        if not raw:
            return None
        try:
            return float(raw)
        except ValueError:
            return None

    def _sleep_backoff(self, attempt: int) -> None:
        delay = min(self.backoff_base ** (attempt + 1), self.max_backoff)
        delay += random.uniform(0, delay * 0.1)  # jitter
        time.sleep(delay)


def paginate(
    client: CircleClient,
    path: str,
    params: dict | None = None,
    *,
    per_page: int = 100,
    max_pages: int | None = None,
) -> Iterator[dict]:
    """Walk Circle's page/per_page envelope.

    Iteration is driven by ``has_next_page`` rather than a computed page count,
    because the docs warn that ``count`` shifts as content is added or removed.
    """
    page = 1
    params = dict(params or {})
    while True:
        params.update({"page": page, "per_page": per_page})
        payload = client.get(path, params=params)

        if isinstance(payload, list):
            # A few endpoints return a bare array and are not paginated.
            yield from payload
            return

        records = payload.get("records")
        if records is None:
            # Unpaginated object response.
            yield payload
            return

        yield from records

        if not payload.get("has_next_page"):
            return
        page += 1
        if max_pages is not None and page > max_pages:
            logger.info("Stopped paginating %s at page limit %d", path, max_pages)
            return


def paginate_chat_messages(
    client: CircleClient,
    chat_room_uuid: str,
    *,
    batch: int = 100,
    max_batches: int = 20,
) -> Iterator[dict]:
    """Walk chat-room messages, which use cursor pagination, not page/per_page.

    The endpoint takes a reference message ``id`` and walks backwards via
    ``previous_per_page``. It is a different code path from every other list.
    """
    path = f"/api/headless/v1/messages/{chat_room_uuid}/chat_room_messages"
    cursor: str | None = None
    seen: set[str] = set()

    for _ in range(max_batches):
        params: dict[str, Any] = {"previous_per_page": batch, "next_per_page": 0}
        if cursor:
            params["id"] = cursor

        payload = client.get(path, params=params)
        messages = (
            payload if isinstance(payload, list) else payload.get("records") or []
        )
        if not messages:
            return

        fresh = [m for m in messages if str(m.get("id")) not in seen]
        if not fresh:
            return
        for m in fresh:
            seen.add(str(m.get("id")))
            yield m

        # Oldest message in this batch becomes the next cursor.
        ids = [m.get("id") for m in fresh if m.get("id") is not None]
        if not ids:
            return
        oldest = min(ids, key=lambda x: int(x) if str(x).isdigit() else 0)
        if str(oldest) == str(cursor):
            return
        cursor = str(oldest)
        if len(fresh) < batch:
            return
