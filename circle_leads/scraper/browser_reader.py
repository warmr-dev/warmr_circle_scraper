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
import re
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
        # One profile per community, so each community's login is isolated and
        # persists independently on your machine.
        base_dir = Path(profile_dir) if profile_dir else DEFAULT_PROFILE_DIR
        self.profile_dir = base_dir / self.community_host
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

    class LoginChallenged(RuntimeError):
        """Circle showed a bot/verification or 2FA wall during login."""

    def login_with_credentials(
        self, email: str, password: str, *, timeout_seconds: int = 120,
        headless: bool = True,
    ) -> None:
        """Sign in by typing the given credentials into Circle's login page.

        The user opted to store credentials instead of logging in by hand. This
        types them locally into Circle's own form; they are never logged or
        uploaded. If Circle presents a Cloudflare or 2FA challenge, this STOPS
        and raises LoginChallenged rather than trying to defeat it.
        """
        # Reuse the remote-browser challenge detector so the policy is identical.
        from circle_leads.remote_browser.session import detect_challenge

        with self._sync_playwright() as pw:
            ctx = pw.chromium.launch_persistent_context(
                str(self.profile_dir), headless=headless
            )
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            try:
                # Circle routes unauthenticated members through /sign_in.
                page.goto(f"{self.base}/sign_in?request_host={self.community_host}",
                          wait_until="domcontentloaded", timeout=45_000)

                # Already have a session in this profile? Nothing to do.
                if self._is_logged_in_via(page):
                    return

                marker = detect_challenge(page.url, self._safe_content(page))
                if marker:
                    raise self.LoginChallenged(
                        f"Circle presented a verification challenge ({marker}) before "
                        "the login form. Stopping rather than trying to bypass it. "
                        "Log in interactively instead: circle-connector login "
                        f"{self.community_host}"
                    )

                self._fill_login_form(page, email, password)

                # Wait for the session to become valid, watching for a challenge
                # or a 2FA prompt that appears after submit.
                deadline = time.time() + timeout_seconds
                while time.time() < deadline:
                    if self._is_logged_in_via(page):
                        return
                    marker = detect_challenge(page.url, self._safe_content(page))
                    if marker:
                        raise self.LoginChallenged(
                            f"Circle presented a challenge ({marker}) after submitting "
                            "the login. This account needs an interactive login: "
                            f"circle-connector login {self.community_host}"
                        )
                    time.sleep(2)
                raise NotLoggedIn(
                    "Login submitted but no valid session appeared. Circle may "
                    "require an email code or 2FA — log in interactively: "
                    f"circle-connector login {self.community_host}"
                )
            finally:
                ctx.close()

    @staticmethod
    def _safe_content(page) -> str:
        try:
            return page.content()
        except Exception:
            return ""

    def _fill_login_form(self, page, email: str, password: str) -> None:
        """Type credentials into Circle's login form. Never logs the values."""
        # Circle's form fields are conventional; try the common selectors.
        email_sel = (
            "input[type='email'], input[name='user[email]'], "
            "input[name='email'], input#email"
        )
        pass_sel = (
            "input[type='password'], input[name='user[password]'], "
            "input[name='password'], input#password"
        )
        try:
            page.fill(email_sel, email, timeout=15_000)
        except Exception as exc:  # noqa: BLE001
            raise NotLoggedIn(
                "Could not find the email field on Circle's login page. Log in "
                f"interactively: circle-connector login {self.community_host}"
            ) from exc
        # Some Circle communities gate the password behind a "Continue" step.
        if page.locator(pass_sel).count() == 0:
            for label in ("Continue", "Next", "Sign in"):
                btn = page.get_by_role("button", name=re.compile(label, re.I))
                if btn.count():
                    btn.first.click()
                    break
            page.wait_for_timeout(1500)
        try:
            page.fill(pass_sel, password, timeout=15_000)
        except Exception as exc:  # noqa: BLE001
            raise NotLoggedIn(
                "Could not find the password field on Circle's login page. Log in "
                f"interactively: circle-connector login {self.community_host}"
            ) from exc
        # Submit.
        for label in ("Sign in", "Log in", "Continue"):
            btn = page.get_by_role("button", name=re.compile(rf"^{label}$", re.I))
            if btn.count():
                btn.first.click()
                return
        page.keyboard.press("Enter")

    # Endpoints that return 200 only for a signed-in member. Communities expose
    # slightly different internal routes, so several are tried.
    _LOGIN_CHECK_PATHS = (
        "/internal_api/current_community_member",
        "/internal_api/community_members/me",
        "/internal_api/me",
    )

    def _is_logged_in_via(self, page) -> bool:
        """True when the browser has a valid member session for this community.

        Tries member-only API endpoints first; falls back to a DOM check for a
        signed-in shell (no visible "Sign in" affordance).
        """
        for path in self._LOGIN_CHECK_PATHS:
            try:
                resp = page.request.get(
                    self.base + path, headers={"Accept": "application/json"}
                )
                if resp.status == 200:
                    return True
                if resp.status in (401, 403):
                    return False  # definitively signed out
            except Exception:
                continue
        # Fallback: a logged-in Circle page renders no top-level "Sign in" link.
        try:
            html = page.content().lower()
            signed_out = "sign in" in html and "internal_api" not in html
            return not signed_out
        except Exception:
            return False

    def status(self) -> bool:
        """Return whether this community has a usable saved session."""
        with self._sync_playwright() as pw:
            ctx = pw.chromium.launch_persistent_context(
                str(self.profile_dir), headless=True
            )
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            try:
                page.goto(self.base, wait_until="domcontentloaded", timeout=20000)
                ok = self._is_logged_in_via(page)
            except Exception:
                ok = False
            ctx.close()
            return ok

    def export_session_cookies(self) -> dict:
        """Return the member session cookies from the logged-in profile.

        These drive the HTTP MemberApiReader, so the browser is needed only for
        the one-time login: after this, reads are plain HTTP. Returns only the
        session cookies (not cf_clearance / analytics). Raises NotLoggedIn if
        there is no valid session to export.
        """
        from circle_leads.scraper.member_api_reader import SESSION_COOKIE_NAMES

        with self._sync_playwright() as pw:
            ctx = pw.chromium.launch_persistent_context(
                str(self.profile_dir), headless=True
            )
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            try:
                page.goto(self.base, wait_until="domcontentloaded", timeout=20000)
                if not self._is_logged_in_via(page):
                    ctx.close()
                    raise NotLoggedIn(
                        f"No valid session for {self.community_host} to export."
                    )
                jar = ctx.cookies()
            finally:
                if ctx:
                    ctx.close()
        out = {}
        for c in jar:
            if c.get("name") in SESSION_COOKIE_NAMES:
                out[c["name"]] = c["value"]
        if not out:
            raise NotLoggedIn(
                f"Logged in to {self.community_host} but found no session cookie "
                "to export."
            )
        return out

    def logout(self) -> None:
        """Clear this community's saved session by wiping its profile cookies."""
        import shutil

        if self.profile_dir.exists():
            shutil.rmtree(self.profile_dir, ignore_errors=True)
        self.profile_dir.mkdir(parents=True, exist_ok=True)

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

    def list_spaces(self) -> list[dict]:
        """Enumerate the spaces this member can access, via the logged-in browser.

        Uses ``/internal_api/spaces`` -- the same call Circle's web app makes to
        render the sidebar -- so it returns exactly the spaces your account can
        see, no more. Raises NotLoggedIn if the session isn't valid.
        """
        with self._sync_playwright() as pw:
            ctx = pw.chromium.launch_persistent_context(
                str(self.profile_dir), headless=self.headless
            )
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto(self.base, wait_until="domcontentloaded")
            if not self._is_logged_in_via(page):
                ctx.close()
                raise NotLoggedIn(
                    f"No valid session for {self.community_host}. Run login first."
                )
            payload = self._fetch_json(page, "/internal_api/spaces")
            ctx.close()
        records = (
            payload.get("records") if isinstance(payload, dict) else payload
        ) or []
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
