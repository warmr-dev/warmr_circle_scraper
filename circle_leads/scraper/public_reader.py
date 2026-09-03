"""Read publicly accessible Circle community spaces -- no login required.

Some communities expose spaces publicly: their ``/internal_api/spaces`` list and
certain spaces' posts return data to anyone, no membership or cookie needed
(e.g. a public "I need a consultant" board). This reads only those genuinely
public endpoints -- if a space returns 401/403, it is private and skipped.

This is ordinary public-web reading: the same requests a logged-out visitor's
browser makes. It reads only what the community chose to publish, at a polite
rate, and never authenticates as anyone.
"""

from __future__ import annotations

import logging
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


@dataclass
class PublicSpace:
    id: str
    slug: str
    name: str
    is_public: bool | None = None  # None until probed


@dataclass
class PublicReader:
    """Reads a Circle community's publicly available spaces and posts."""

    community_host: str
    requests_per_minute: int = 40
    timeout: int = 20
    session: requests.Session = field(default_factory=requests.Session)
    _last: float = 0.0

    def __post_init__(self) -> None:
        self.community_host = self.community_host.replace("https://", "").strip("/")
        self.base = f"https://{self.community_host}"

    def _throttle(self) -> None:
        interval = 60.0 / max(1, self.requests_per_minute)
        wait = interval - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()

    def _get(self, path: str) -> tuple[int, dict | list | None]:
        self._throttle()
        try:
            resp = self.session.get(
                self.base + path,
                headers={"User-Agent": BROWSER_UA, "Accept": "application/json"},
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            logger.debug("Public fetch failed for %s: %s", path, exc.__class__.__name__)
            return 0, None
        if resp.status_code != 200:
            return resp.status_code, None
        try:
            return 200, resp.json()
        except ValueError:
            return 200, None

    def list_spaces(self) -> list[PublicSpace]:
        """List the community's spaces, if its space list is public."""
        status, payload = self._get("/internal_api/spaces")
        if status != 200 or not payload:
            return []
        records = (
            payload.get("records") if isinstance(payload, dict) else payload
        ) or []
        out = []
        for s in records:
            if not isinstance(s, dict) or s.get("id") is None:
                continue
            out.append(
                PublicSpace(
                    id=str(s["id"]),
                    slug=str(s.get("slug") or ""),
                    name=str(s.get("name") or s.get("slug") or ""),
                )
            )
        return out

    def read_comments(self, post_id: str | int) -> list[dict]:
        """Read a post's public comments (hiring asks often appear as replies)."""
        status, payload = self._get(f"/internal_api/posts/{post_id}/comments")
        if status != 200 or not payload:
            return []
        return (payload.get("records") if isinstance(payload, dict) else payload) or []

    def read_space(
        self, space_id: str | int, *, per_page: int = 20, max_pages: int = 10,
        since=None,
    ) -> tuple[bool, list[dict]]:
        """Read a space's posts if public. Returns (was_public, records).

        Posts come back newest-first. When ``since`` (a datetime) is given, we
        stop as soon as we reach a post older than it -- so a recency window or
        an incremental re-read only fetches fresh posts, not the whole history.
        """
        from circle_leads.scraper.normalize import parse_timestamp

        records: list[dict] = []
        for page in range(1, max_pages + 1):
            status, payload = self._get(
                f"/internal_api/spaces/{space_id}/posts"
                f"?page={page}&per_page={per_page}&sort=latest"
            )
            if status in (401, 403):
                return False, []  # private space
            if status != 200 or not payload:
                break
            batch = (
                payload.get("records") if isinstance(payload, dict) else payload
            ) or []
            if not batch:
                break

            if since is not None:
                fresh = []
                hit_old = False
                for post in batch:
                    ts = parse_timestamp(post.get("created_at") or post.get("published_at"))
                    if ts is not None and ts <= since:
                        hit_old = True
                        break
                    fresh.append(post)
                records.extend(fresh)
                if hit_old:
                    break  # everything after this is older; stop paging
            else:
                records.extend(batch)

            if isinstance(payload, dict) and not payload.get("has_next_page"):
                break
        return True, records


def _tiptap_text(node) -> str:
    """Flatten Circle's TipTap rich-text JSON (used by comments) to plain text."""
    out = []
    if isinstance(node, dict):
        if node.get("type") == "text" and node.get("text"):
            out.append(node["text"])
        for child in node.get("content", []) or []:
            out.append(_tiptap_text(child))
    return " ".join(filter(None, out))


def _extract_text(record: dict) -> tuple[str, str]:
    title = strip_html(record.get("name") or record.get("title") or "")
    body = strip_html(record.get("truncated_content") or "")
    if not body:
        for key in ("body_plain_text", "plain_text", "content"):
            if record.get(key):
                body = strip_html(str(record[key]))
                break
    # Comments carry their text in tiptap_body rather than truncated_content.
    if not body and isinstance(record.get("tiptap_body"), dict):
        tb = record["tiptap_body"]
        # The doc may be nested under a "body" key.
        node = tb.get("body") if isinstance(tb.get("body"), dict) else tb
        body = strip_html(_tiptap_text(node))
    return title, body


def normalize_public_post(
    record: dict, *, community_url: str, excluded_content: list[str] | None = None
) -> dict | None:
    pid = record.get("id") or record.get("post_id")
    if pid is None:
        return None
    title, body = _extract_text(record)
    text = f"{title}\n\n{body}".strip() if title and title != body else (body or title)
    text = redact_pii(text, excluded_content)
    if not text.strip():
        return None
    url = record.get("url")
    if url and url.startswith("/"):
        url = community_url.rstrip("/") + url
    author = record.get("user") or record.get("community_member") or {}
    return {
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
        "permission_reference": "public_space",
    }


def discover_and_read_public(
    reader: PublicReader,
    *,
    space_ids: list[str] | None = None,
    only_lead_spaces: bool = False,
    excluded_content: list[str] | None = None,
    max_pages: int = 5,
    include_comments: bool = False,
) -> tuple[list[PublicSpace], list[dict]]:
    """Enumerate a community's public spaces and read the readable ones.

    If ``space_ids`` is given, only those are read. If ``only_lead_spaces``,
    spaces whose name/slug suggest hiring/requests are prioritized. Returns
    (spaces_with_public_flag, normalized_records).
    """
    spaces = reader.list_spaces()
    if not spaces:
        return [], []

    targets = spaces
    if space_ids:
        wanted = {str(s) for s in space_ids}
        targets = [s for s in spaces if s.id in wanted or s.slug in wanted]
    elif only_lead_spaces:
        hints = ("consultant", "project", "job", "hir", "need", "opportunit",
                 "collab", "client", "gig", "freelance", "request")
        targets = [
            s for s in spaces
            if any(h in (s.slug + " " + s.name).lower() for h in hints)
        ] or spaces

    records: list[dict] = []
    for sp in targets:
        was_public, raw = reader.read_space(sp.id, max_pages=max_pages)
        sp.is_public = was_public
        if not was_public:
            continue
        for r in raw:
            rec = normalize_public_post(
                r, community_url=reader.base, excluded_content=excluded_content
            )
            if rec:
                records.append(rec)
            # A hiring request is often a comment on someone else's post.
            if include_comments and r.get("comments_count"):
                for c in reader.read_comments(r.get("id")):
                    crec = normalize_public_post(
                        c, community_url=reader.base, excluded_content=excluded_content
                    )
                    if crec:
                        crec["content_type"] = "comment"
                        crec["thread_id"] = str(r.get("id"))
                        records.append(crec)
    return spaces, records
