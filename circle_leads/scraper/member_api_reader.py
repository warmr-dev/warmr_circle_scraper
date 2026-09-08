"""Read a member's private Circle community over plain HTTP with their session.

The important discovery this is built on: Circle's ``/internal_api/*`` JSON
endpoints are NOT behind the Cloudflare challenge that gates the HTML pages.
A valid member session cookie sent to those endpoints with an ordinary HTTP
client returns real JSON (200 when authorized, 401 when not) -- no browser, no
Turnstile, no ``cf_clearance`` required. Verified 2026-09-08 against live
communities.

So the browser is only needed ONCE, to establish the session (Circle's login
page carries Turnstile, which the member clears themselves during a normal
login). After that, every read is a cheap HTTP call that runs anywhere --
Railway, Vercel, a cron box -- with no Chromium at all.

This reader takes the session cookie(s) and does the reads. Where the cookie
comes from is the caller's business: exported from the member's own browser
after they log in, or lifted from a local Playwright profile the member signed
into. This module never logs in and never handles a password.

Security: the session cookie is a live member credential. It is held in memory
for the duration of a read and is never logged. Callers that persist it must
encrypt it (see the connector's credential handling).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

import requests

from circle_leads.scraper.normalize import parse_timestamp, redact_pii, strip_html

logger = logging.getLogger(__name__)

# A normal desktop UA. Not spoofing anything special -- the API doesn't gate on
# it; this just avoids a default python-requests UA that some WAFs flag.
_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")

# Cookies that carry a member session. cf_clearance is deliberately NOT here --
# the API does not require it (proven), and it is IP/device-bound anyway.
SESSION_COOKIE_NAMES = ("_circle_session", "remember_user_token", "user_session_identifier")


class SessionInvalid(RuntimeError):
    """The session cookie is missing/expired -- Circle returned 401/403."""


class ChallengeHit(RuntimeError):
    """Unexpectedly hit a Cloudflare challenge on an API call (should not happen
    for /internal_api, but reported rather than worked around if it does)."""


@dataclass
class MemberApiReader:
    """Reads a Circle community's spaces and posts over HTTP with a session."""

    host: str
    cookies: dict          # {"_circle_session": "...", ...}
    request_pause: float = 0.7
    _http: requests.Session = field(default=None, repr=False)

    def __post_init__(self):
        self.host = self.host.replace("https://", "").replace("http://", "").strip("/")
        self.base = f"https://{self.host}"
        self._http = requests.Session()
        self._http.headers.update({"User-Agent": _UA, "Accept": "application/json"})
        # Only pass known session cookies; ignore analytics/cf junk.
        for name in SESSION_COOKIE_NAMES:
            if self.cookies.get(name):
                self._http.cookies.set(name, self.cookies[name], domain=self.host)

    def __repr__(self) -> str:  # never leak cookie values
        return f"MemberApiReader(host={self.host!r}, cookies=<{len(self.cookies)} redacted>)"

    # --- low level --------------------------------------------------------

    def _get(self, path: str) -> dict | list | None:
        r = self._http.get(self.base + path, timeout=30, allow_redirects=False)
        # A challenge would be HTML with cf markers; the API returns JSON.
        ctype = r.headers.get("content-type", "")
        if "application/json" not in ctype:
            if "__cf_chl" in r.text or "challenge-platform" in r.text:
                raise ChallengeHit(
                    f"Unexpected Cloudflare challenge on {path}. The API path "
                    "normally isn't gated; stopping rather than working around it."
                )
            return None  # 404 SPA shell or similar
        if r.status_code in (401, 403):
            raise SessionInvalid(
                f"Circle rejected the session on {path} ({r.status_code}). "
                "The cookie is missing or expired -- re-authenticate."
            )
        if r.status_code >= 400:
            return None
        try:
            return r.json()
        except ValueError:
            return None

    # --- session check ----------------------------------------------------

    def check_session(self) -> bool:
        """True if the session can read this community's spaces."""
        try:
            payload = self._get("/internal_api/spaces")
        except SessionInvalid:
            return False
        return bool(payload)

    # --- reads ------------------------------------------------------------

    def list_spaces(self) -> list[dict]:
        """Spaces this member can access."""
        payload = self._get("/internal_api/spaces")
        records = (payload.get("records") if isinstance(payload, dict) else payload) or []
        out = []
        for sp in records:
            if isinstance(sp, dict) and sp.get("id") is not None:
                out.append({
                    "id": str(sp["id"]),
                    "slug": str(sp.get("slug") or ""),
                    "name": str(sp.get("name") or sp.get("slug") or ""),
                    "space_type": sp.get("space_type") or sp.get("post_type"),
                })
        return out

    def list_posts(self, space_id: str | int, *, per_page: int = 20,
                   max_pages: int = 10) -> list[dict]:
        """Posts in a space, paginated."""
        records: list[dict] = []
        for pageno in range(1, max_pages + 1):
            path = (f"/internal_api/spaces/{space_id}/posts"
                    f"?page={pageno}&per_page={per_page}&sort=latest")
            payload = self._get(path)
            if not payload:
                break
            batch = payload.get("records") if isinstance(payload, dict) else payload
            if not batch:
                break
            records.extend(batch)
            if isinstance(payload, dict) and not payload.get("has_next_page"):
                break
            time.sleep(self.request_pause)
        return records

    def list_comments(self, post_id: str | int, *, per_page: int = 30,
                      max_pages: int = 3) -> list[dict]:
        """Top-level comments on a post (verified endpoint)."""
        records: list[dict] = []
        for pageno in range(1, max_pages + 1):
            payload = self._get(
                f"/internal_api/posts/{post_id}/comments?page={pageno}&per_page={per_page}")
            batch = (payload.get("records") if isinstance(payload, dict) else payload) or []
            if not batch:
                break
            records.extend(batch)
            if isinstance(payload, dict) and not payload.get("has_next_page"):
                break
            time.sleep(self.request_pause)
        return records

    def list_replies(self, comment_id: str | int, *, per_page: int = 30) -> list[dict]:
        """Replies to a comment (nested comments)."""
        payload = self._get(
            f"/internal_api/comments/{comment_id}/comments?per_page={per_page}")
        return (payload.get("records") if isinstance(payload, dict) else payload) or []


def _post_id(record: dict):
    return record.get("id") or record.get("post_id")


def _tiptap_text(node) -> str:
    """Flatten Circle's tiptap/ProseMirror JSON body to plain text.

    Posts and comments carry the full body as tiptap_body (structured JSON);
    truncated_content is only a preview. Walking the tree gets the whole text.
    """
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    out: list[str] = []
    if isinstance(node, dict):
        if node.get("type") == "text" and isinstance(node.get("text"), str):
            out.append(node["text"])
        for child in (node.get("content") or []):
            out.append(_tiptap_text(child))
        # Block nodes -> newline so paragraphs don't run together.
        if node.get("type") in ("paragraph", "heading", "listItem", "blockquote"):
            out.append("\n")
    elif isinstance(node, list):
        for child in node:
            out.append(_tiptap_text(child))
    return "".join(out)


def _extract_text(record: dict) -> tuple[str, str]:
    title = strip_html(record.get("name") or record.get("title") or "")
    # Prefer the full tiptap body over the truncated preview.
    body = _tiptap_text(record.get("tiptap_body")).strip()
    if not body:
        body = strip_html(record.get("truncated_content") or "")
    if not body:
        for key in ("body_plain_text", "plain_text", "content", "text"):
            if record.get(key):
                body = strip_html(str(record[key]))
                break
    return title, body


def _comment_author(record: dict) -> dict:
    a = record.get("community_member") or record.get("user") or {}
    return {
        "source_author_id": a.get("id"),
        "display_name": a.get("name") or a.get("full_name"),
        "profile_url": a.get("url"),
    }


def fetch_space_posts(reader: MemberApiReader, space_id: str | int, *,
                      excluded_content: list[str] | None = None,
                      max_pages: int = 10,
                      with_comments: bool = True,
                      max_comment_posts: int = 25) -> list[dict]:
    """Read + normalize a space's posts (and their comments + replies) for the
    lead pipeline. Same record shape as the browser reader, so callers are
    interchangeable.

    ``with_comments`` also pulls comments and their replies for up to
    ``max_comment_posts`` posts -- hiring intent often lives in a comment
    ("DM me", "we're looking for…") as much as the post body.
    """
    community_url = reader.base
    records: list[dict] = []
    posts = reader.list_posts(space_id, max_pages=max_pages)
    for i, record in enumerate(posts):
        pid = _post_id(record)
        if pid is None:
            continue
        title, body = _extract_text(record)
        text = (f"{title}\n\n{body}".strip()
                if title and title != body else (body or title))
        text = redact_pii(text, excluded_content)
        if text.strip():
            url = record.get("url")
            if url and url.startswith("/"):
                url = community_url.rstrip("/") + url
            author = record.get("user") or record.get("community_member") or {}
            records.append({
                "source_content_id": str(pid),
                "content_type": "post",
                "thread_id": str(pid),
                "title": title or None,
                "content": text,
                "url": url,
                "published_at": parse_timestamp(
                    record.get("created_at") or record.get("published_at")),
                "author": {
                    "source_author_id": author.get("id"),
                    "display_name": author.get("name") or author.get("full_name"),
                    "profile_url": author.get("url"),
                },
                "permission_reference": "member_session",
            })

        # Comments + replies (bounded, so a huge thread can't dominate a scan).
        if with_comments and i < max_comment_posts and record.get("comments_count") != 0:
            try:
                comments = reader.list_comments(pid, max_pages=2)
            except (SessionInvalid, ChallengeHit):
                raise
            except Exception:  # noqa: BLE001 - a comment fetch must not kill the scan
                comments = []
            for cm in comments:
                records.extend(_normalize_comment(
                    reader, cm, thread_id=str(pid), community_url=community_url,
                    excluded_content=excluded_content))
    return records


def _normalize_comment(reader, comment: dict, *, thread_id: str,
                       community_url: str, excluded_content) -> list[dict]:
    """Normalize a comment plus its replies into lead-pipeline records."""
    out: list[dict] = []
    cid = comment.get("id")
    if cid is None:
        return out
    _, body = _extract_text(comment)   # comments have no title
    body = redact_pii(body, excluded_content)
    if body.strip():
        url = comment.get("show_url") or comment.get("url")
        if url and url.startswith("/"):
            url = community_url.rstrip("/") + url
        out.append({
            "source_content_id": f"c{cid}",
            "content_type": "comment",
            "thread_id": thread_id,
            "title": None,
            "content": body,
            "url": url,
            "published_at": parse_timestamp(comment.get("created_at")),
            "author": _comment_author(comment),
            "permission_reference": "member_session",
        })
    # Replies (nested comments).
    if (comment.get("replies_count") or 0) > 0:
        try:
            replies = reader.list_replies(cid)
        except Exception:  # noqa: BLE001
            replies = []
        for rp in replies:
            rid = rp.get("id")
            if rid is None:
                continue
            _, rbody = _extract_text(rp)
            rbody = redact_pii(rbody, excluded_content)
            if not rbody.strip():
                continue
            out.append({
                "source_content_id": f"c{rid}",
                "content_type": "comment",
                "thread_id": thread_id,
                "title": None,
                "content": rbody,
                "url": comment.get("show_url"),
                "published_at": parse_timestamp(rp.get("created_at")),
                "author": _comment_author(rp),
                "permission_reference": "member_session",
            })
    return out
