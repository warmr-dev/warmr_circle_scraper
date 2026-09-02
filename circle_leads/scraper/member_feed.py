"""Read spaces you are a member of via Circle's internal feed endpoints.

This is for communities where you are a genuine, logged-in member and want to
read your own feed automatically. It calls the same ``/internal_api/`` endpoints
your browser calls, authenticated by your session cookie.

Important boundaries, by design:

- These are Circle's *internal* endpoints, not a documented API. Circle's terms
  discourage automated access to them, so this is a member reading their own
  feeds at a polite rate -- not a crawler. It is read-only, stops on the first
  401/403, and rate-limits itself.
- Your session cookie is read from an environment variable at call time. It is
  never written to disk, never logged, and never committed. You copy it once
  from your browser (see ``docs/member_feed.md``).
- Only spaces you name are read. Nothing is joined, posted, or changed.

If Circle changes these endpoints or blocks the session, that is the expected
failure mode -- the internal API carries no stability guarantee.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from typing import Iterator

import requests

from circle_leads.scraper.normalize import parse_timestamp, redact_pii, strip_html

logger = logging.getLogger(__name__)

BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)


class SessionExpired(RuntimeError):
    """Raised on 401/403 -- the session cookie is missing or stale. Stop."""


@dataclass
class MemberFeedClient:
    """Reads a member's own Circle feeds through the internal endpoints."""

    community_host: str  # e.g. "startupandangels.circle.so"
    cookie: str  # the full Cookie header value, from the environment
    requests_per_minute: int = 30
    timeout: int = 20
    session: requests.Session = field(default_factory=requests.Session)
    _last_request: float = 0.0

    def __post_init__(self) -> None:
        self.community_host = self.community_host.replace("https://", "").strip("/")
        self.base = f"https://{self.community_host}"

    @classmethod
    def from_env(cls, community_host: str, cookie_env: str, **kw) -> "MemberFeedClient":
        cookie = os.environ.get(cookie_env, "").strip()
        if not cookie:
            raise SessionExpired(
                f"No session cookie in ${cookie_env}. Copy your Cookie header "
                "from the browser (see docs/member_feed.md) and set that variable."
            )
        return cls(community_host=community_host, cookie=cookie, **kw)

    def _throttle(self) -> None:
        min_interval = 60.0 / max(1, self.requests_per_minute)
        wait = min_interval - (time.monotonic() - self._last_request)
        if wait > 0:
            time.sleep(wait)
        self._last_request = time.monotonic()

    def _get(self, path: str, params: dict | None = None) -> dict | list:
        self._throttle()
        resp = self.session.get(
            self.base + path,
            params=params,
            headers={
                "Accept": "application/json",
                "User-Agent": BROWSER_UA,
                "Cookie": self.cookie,
                "X-Requested-With": "XMLHttpRequest",
            },
            timeout=self.timeout,
        )
        if resp.status_code in (401, 403):
            # A stale session is a stop condition, not something to retry.
            raise SessionExpired(
                f"Session rejected ({resp.status_code}) for {self.community_host}. "
                "Refresh your cookie from the browser."
            )
        resp.raise_for_status()
        return resp.json()

    def list_posts(
        self, space_id: str | int, *, per_page: int = 20, max_pages: int = 10
    ) -> Iterator[dict]:
        """Yield raw post summaries from a space's feed, page by page."""
        page = 1
        while page <= max_pages:
            payload = self._get(
                f"/internal_api/spaces/{space_id}/posts",
                {"page": page, "per_page": per_page, "sort": "latest"},
            )
            records = payload.get("records") if isinstance(payload, dict) else payload
            if not records:
                return
            yield from records
            if isinstance(payload, dict) and not payload.get("has_next_page"):
                return
            page += 1

    def post_details(self, post_ids: list[str | int], space_id: str | int) -> list[dict]:
        """Fetch full bodies for a batch of post ids."""
        if not post_ids:
            return []
        ids = ",".join(str(p) for p in post_ids)
        payload = self._get(
            "/internal_api/post_details", {"post_ids": ids, "space_id": space_id}
        )
        if isinstance(payload, list):
            return payload
        return payload.get("posts") or payload.get("records") or []


def _post_id(record: dict):
    return record.get("id") or record.get("post_id")


def _extract_text(record: dict) -> tuple[str, str]:
    """Return (title, body) from an internal-feed post record.

    Circle's internal feed puts the title in ``name`` and the (truncated) body
    in ``truncated_content``. The list response does not carry the full body --
    ``show_more: true`` marks a post whose text is cut -- but the title plus the
    first ~255 chars is enough to classify hiring intent. Falls back to the
    several other body shapes for robustness.
    """
    title = strip_html(record.get("name") or record.get("title") or "")
    body = strip_html(record.get("truncated_content") or "")
    if not body:
        for key in ("body_plain_text", "plain_text", "content", "text"):
            if record.get(key):
                body = strip_html(str(record[key]))
                break
    if not body and isinstance(record.get("body"), dict):
        b = record["body"]
        body = strip_html(str(b.get("body") or b.get("plain_text") or b.get("text") or ""))
    if not body and isinstance(record.get("tiptap_body"), dict):
        body = strip_html(_tiptap_text(record["tiptap_body"]))
    return title, body


def _tiptap_text(node: dict) -> str:
    """Flatten Circle's TipTap rich-text JSON into plain text."""
    out = []
    if isinstance(node, dict):
        if node.get("type") == "text" and node.get("text"):
            out.append(node["text"])
        for child in node.get("content", []) or []:
            out.append(_tiptap_text(child))
    return " ".join(filter(None, out))


def fetch_space_posts(
    client: MemberFeedClient,
    space_id: str | int,
    *,
    community_url: str | None = None,
    excluded_content: list[str] | None = None,
    max_pages: int = 10,
) -> list[dict]:
    """Read a space's posts and normalize them for the triage/lead pipeline.

    Returns records shaped like the rest of the pipeline expects, so they flow
    straight into classification and scoring.
    """
    # The list response already carries title (name) and body
    # (truncated_content); post_details returns only metadata, so it is not
    # fetched. Truncated body + title is enough to classify hiring intent.
    summaries = list(client.list_posts(space_id, max_pages=max_pages))

    records: list[dict] = []
    for record in summaries:
        pid = _post_id(record)
        if pid is None:
            continue
        title, body = _extract_text(record)
        text = f"{title}\n\n{body}".strip() if title and title != body else (body or title)
        text = redact_pii(text, excluded_content)
        if not text.strip():
            continue

        url = record.get("url")
        if url and community_url and url.startswith("/"):
            url = community_url.rstrip("/") + url

        author = record.get("user") or record.get("community_member") or {}
        records.append(
            {
                "source_content_id": str(pid),
                "content_type": "post",
                "thread_id": str(pid),
                "title": title or None,
                "content": text,
                "url": url,
                "published_at": parse_timestamp(
                    record.get("created_at") or record.get("published_at")
                ),
                "author": {
                    "source_author_id": author.get("id"),
                    "display_name": author.get("name") or author.get("full_name"),
                    "profile_url": author.get("url"),
                },
                "permission_reference": "member_feed",
            }
        )
    return records
