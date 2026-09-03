"""Read your own Circle feeds through a browser you log into yourself.

This is the safe way to automate reading a community you belong to. It launches
a Chromium browser with a *persistent profile* that lives on your machine. You
log into Circle once, in that window; the session then lives in the browser's
own cookie store -- exactly like your normal browser.

Python never sees, copies, stores, or transmits your session token. The browser
holds it and sends it automatically, the way it does for any page you open.
Nothing is joined, posted, or changed; this only reads feeds you can already
see. Requests go at a browser's natural pace.

First run: a window opens; log in and it saves the session to the profile dir.
Later runs reuse it silently until the login expires, then it prompts again.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Iterator

from circle_leads.scraper.normalize import parse_timestamp, redact_pii, strip_html

logger = logging.getLogger(__name__)

DEFAULT_PROFILE_DIR = Path.home() / ".circle-leads" / "browser-profile"


class BrowserNotAvailable(RuntimeError):
    """Raised when Playwright/Chromium is not installed."""


class NotLoggedIn(RuntimeError):
    """Raised when the profile has no valid Circle session and none was created."""


def _require_playwright():
    try:
        from playwright.sync_api import sync_playwright  # noqa: F401
    except ImportError as exc:  # pragma: no cover
        raise BrowserNotAvailable(
            "Playwright is not installed. Enable the browser reader with:\n"
            "  pip install 'circle-leads[browser]'\n"
            "  playwright install chromium"
        ) from exc
    return sync_playwright


class BrowserFeedReader:
    """Reads Circle feeds using a persistent, user-logged-in browser profile."""

    def __init__(
        self,
        community_host: str,
        *,
        profile_dir: str | Path | None = None,
        headless: bool = True,
        request_pause: float = 1.0,
    ):
        self.community_host = community_host.replace("https://", "").strip("/")
        self.base = f"https://{self.community_host}"
        self.profile_dir = Path(profile_dir or DEFAULT_PROFILE_DIR)
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self.headless = headless
        self.request_pause = request_pause
        self._sync_playwright = _require_playwright()

    def login(self, *, timeout_seconds: int = 300) -> None:
        """Open a visible window so you can log in. Saves the session to profile.

        Run this once (or when the session expires). It waits until you have
        signed in, then closes.
        """
        with self._sync_playwright() as pw:
            ctx = pw.chromium.launch_persistent_context(
                str(self.profile_dir), headless=False
            )
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto(self.base, wait_until="domcontentloaded")
            print(
                f"\n  A browser window opened for {self.community_host}.\n"
                "  Log in there. This waits until you're signed in, then closes.\n"
            )
            deadline = time.time() + timeout_seconds
            while time.time() < deadline:
                if self._is_logged_in_via(page):
                    print("  Signed in. Session saved.\n")
                    ctx.close()
                    return
                time.sleep(2)
            ctx.close()
            raise NotLoggedIn("Timed out waiting for login.")

    def _is_logged_in_via(self, page) -> bool:
        """Heuristic: the member API for the current user returns 200 when in."""
        try:
            resp = page.request.get(
                f"{self.base}/internal_api/current_community_member",
                headers={"Accept": "application/json"},
            )
            return resp.status == 200
        except Exception:
            return False

    def _fetch_json(self, page, path: str) -> dict | list | None:
        """Fetch an internal endpoint using the browser's own session."""
        resp = page.request.get(
            self.base + path, headers={"Accept": "application/json"}
        )
        if resp.status in (401, 403):
            raise NotLoggedIn(
                f"Not logged in to {self.community_host}. Run `read-feed --login` first."
            )
        if resp.status >= 400:
            return None
        try:
            return resp.json()
        except Exception:
            return None

    def list_posts(
        self, space_id: str | int, *, per_page: int = 20, max_pages: int = 10
    ) -> list[dict]:
        """Read a space's posts through the logged-in browser."""
        with self._sync_playwright() as pw:
            ctx = pw.chromium.launch_persistent_context(
                str(self.profile_dir), headless=self.headless
            )
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            # Prime the origin so requests carry the session.
            page.goto(self.base, wait_until="domcontentloaded")
            if not self._is_logged_in_via(page):
                ctx.close()
                raise NotLoggedIn(
                    f"No valid session for {self.community_host}. "
                    "Run with --login to sign in."
                )

            records: list[dict] = []
            for pageno in range(1, max_pages + 1):
                path = (
                    f"/internal_api/spaces/{space_id}/posts"
                    f"?page={pageno}&per_page={per_page}&sort=latest"
                )
                payload = self._fetch_json(page, path)
                if not payload:
                    break
                batch = (
                    payload.get("records") if isinstance(payload, dict) else payload
                )
                if not batch:
                    break
                records.extend(batch)
                if isinstance(payload, dict) and not payload.get("has_next_page"):
                    break
                time.sleep(self.request_pause)

            ctx.close()
            return records


def _post_id(record: dict):
    return record.get("id") or record.get("post_id")


def _extract_text(record: dict) -> tuple[str, str]:
    title = strip_html(record.get("name") or record.get("title") or "")
    body = strip_html(record.get("truncated_content") or "")
    if not body:
        for key in ("body_plain_text", "plain_text", "content", "text"):
            if record.get(key):
                body = strip_html(str(record[key]))
                break
    return title, body


def fetch_space_posts(
    reader: BrowserFeedReader,
    space_id: str | int,
    *,
    excluded_content: list[str] | None = None,
    max_pages: int = 10,
) -> list[dict]:
    """Read and normalize a space's posts for the lead pipeline."""
    community_url = reader.base
    records: list[dict] = []
    for record in reader.list_posts(space_id, max_pages=max_pages):
        pid = _post_id(record)
        if pid is None:
            continue
        title, body = _extract_text(record)
        text = f"{title}\n\n{body}".strip() if title and title != body else (body or title)
        text = redact_pii(text, excluded_content)
        if not text.strip():
            continue
        url = record.get("url")
        if url and url.startswith("/"):
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
                "permission_reference": "browser_session",
            }
        )
    return records
