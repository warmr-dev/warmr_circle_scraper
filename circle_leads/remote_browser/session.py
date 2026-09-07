"""A persistent Chromium that lives on the server, driven by you interactively.

Why this exists
---------------
Circle has no member OAuth (verified against the live OpenAPI specs: the Auth
API mints a member JWT from an ADMIN's Headless token plus an email, the JWT is
locked to one ``community_id``, and no endpoint lists the communities a person
belongs to). So a member-level session can only come from a real browser.

This module keeps that browser **on the server**. You open Circle's own login
page inside it and authenticate there, so the session originates on the server
and is never copied from anywhere. Exporting cookies from another machine and
replaying them here is explicitly out of scope and is not implemented.

Anti-bot posture
----------------
Circle may treat a datacenter IP as suspicious. If it presents a CAPTCHA, a
device check, or a block page, ``detect_challenge`` reports it and the caller
stops. Nothing here tries to look like a different browser, solve a challenge,
or evade a control: no user-agent spoofing, no stealth plugins, no fingerprint
patching. A challenge is a stop signal, not an obstacle to route around.
"""

from __future__ import annotations

import base64
import enum
import logging
import queue
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

# Default lives outside the repo. On Railway, point this at a mounted volume so
# the profile (and therefore the login) survives a redeploy.
DEFAULT_PROFILE_ROOT = Path.home() / ".circle-leads" / "remote-profile"

# Viewport for the interactive view. Big enough for Circle's desktop layout.
VIEWPORT = {"width": 1280, "height": 800}


class RemoteBrowserUnavailable(RuntimeError):
    """Playwright/Chromium is not installed in this environment."""


class ChallengeDetected(RuntimeError):
    """Circle presented a bot/verification challenge.

    Raised so callers halt. It is never caught-and-retried as a way of getting
    past the challenge.
    """


class SessionState(str, enum.Enum):
    """What the remote browser is doing. Deliberately keeps 'a browser is
    running' separate from 'you are signed in' separate from 'Circle is
    challenging us' -- conflating them hides the failure that matters."""

    STOPPED = "stopped"                    # no browser process
    STARTING = "starting"                  # launching Chromium
    AWAITING_LOGIN = "awaiting_login"      # running, no valid Circle session
    AUTHENTICATED = "authenticated"        # a valid member session exists
    CHALLENGED = "challenged"              # Circle showed a bot/verify wall
    ERROR = "error"                        # unexpected failure


# Substrings that indicate a verification wall rather than a normal page. Kept
# broad on purpose: a false positive stops us (safe), a false negative would
# have us hammering a challenge page (not safe).
_CHALLENGE_MARKERS = (
    "cf-challenge", "cf_chl", "just a moment", "checking your browser",
    "attention required", "captcha", "hcaptcha", "recaptcha", "turnstile",
    "unusual traffic", "verify you are human", "are you a robot",
    "access denied", "request blocked", "ddos protection",
)


@dataclass
class SessionStatus:
    """A snapshot of the remote browser, safe to send to the dashboard.

    Carries no cookies, tokens, or credentials -- only state and counts.
    """

    state: str = SessionState.STOPPED.value
    detail: str = ""
    host: str | None = None
    current_url: str | None = None
    member_label: str | None = None
    started_at: float | None = None
    last_checked_at: float | None = None
    challenge_url: str | None = None

    def as_dict(self) -> dict:
        return {
            "state": self.state,
            "detail": self.detail,
            "host": self.host,
            "current_url": self.current_url,
            "member_label": self.member_label,
            "started_at": self.started_at,
            "last_checked_at": self.last_checked_at,
            "challenge_url": self.challenge_url,
        }


def _require_playwright():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover - depends on the image
        raise RemoteBrowserUnavailable(
            "Playwright is not installed. For the remote browser the image "
            "needs:\n  pip install 'circle-leads[browser]'\n"
            "  playwright install --with-deps chromium"
        ) from exc
    return sync_playwright


def detect_challenge(url: str, html: str) -> str | None:
    """Return the marker that identifies a bot/verification wall, else None.

    Pure function so the policy is testable without a browser.
    """
    haystack = f"{url or ''}\n{(html or '')[:200_000]}".lower()
    for marker in _CHALLENGE_MARKERS:
        if marker in haystack:
            return marker
    return None


# Endpoints that answer 200 only for a signed-in member of that community.
_LOGIN_CHECK_PATHS = (
    "/internal_api/current_community_member",
    "/internal_api/community_members/me",
    "/internal_api/me",
)


class _BrowserThread:
    """Runs every Playwright call on one dedicated, long-lived thread.

    Playwright's sync API binds its greenlet to the thread that created the
    driver, so touching it from another thread raises
    ``greenlet.error: cannot switch to a different thread``. A web server
    serves each request from a rotating threadpool, which hits that
    immediately. So the browser gets one thread of its own and callers submit
    closures to it; exceptions are re-raised to the caller.
    """

    def __init__(self):
        self._calls: "queue.Queue[tuple]" = queue.Queue()
        self._thread = threading.Thread(
            target=self._serve, name="remote-browser", daemon=True
        )
        self._thread.start()

    def _serve(self) -> None:
        while True:
            fn, done, box = self._calls.get()
            if fn is None:  # shutdown sentinel
                done.set()
                return
            try:
                box.append(("ok", fn()))
            except BaseException as exc:  # noqa: BLE001 - relayed to the caller
                box.append(("err", exc))
            finally:
                done.set()

    def call(self, fn, *, timeout: float = 120.0):
        """Run ``fn`` on the browser thread and return its result."""
        done = threading.Event()
        box: list = []
        self._calls.put((fn, done, box))
        if not done.wait(timeout):
            raise TimeoutError("The remote browser did not respond in time.")
        kind, value = box[0]
        if kind == "err":
            raise value
        return value

    def shutdown(self) -> None:
        done = threading.Event()
        self._calls.put((None, done, []))
        done.wait(timeout=10)


class RemoteBrowser:
    """Owns one persistent Chromium and serializes access to it.

    Playwright's sync API is not thread-safe, and a web server is multi-
    threaded, so every browser touch runs on one dedicated worker thread and
    callers hand it closures. That keeps the browser single-threaded without
    making the API async.
    """

    def __init__(
        self,
        *,
        profile_dir: str | Path | None = None,
        headless: bool = True,
        request_pause: float = 1.0,
    ):
        self.profile_dir = Path(profile_dir or DEFAULT_PROFILE_ROOT)
        self.headless = headless
        self.request_pause = request_pause
        self.status = SessionStatus()

        self._lock = threading.RLock()
        self._worker: _BrowserThread | None = None
        self._pw = None
        self._ctx = None
        self._page = None

    def _submit(self, fn, *, timeout: float = 120.0):
        """Run ``fn`` on the dedicated browser thread."""
        with self._lock:
            if self._worker is None:
                self._worker = _BrowserThread()
            worker = self._worker
        return worker.call(fn, timeout=timeout)

    # --- lifecycle --------------------------------------------------------

    def start(self, host: str | None = None) -> SessionStatus:
        """Launch the persistent browser (idempotent) and open ``host``."""
        _require_playwright()  # fail fast, on the caller's thread
        return self._submit(lambda: self._start_locked(host))

    def _start_locked(self, host: str | None) -> SessionStatus:
        with self._lock:
            sync_playwright = _require_playwright()
            if self._ctx is None:
                self.status.state = SessionState.STARTING.value
                self.profile_dir.mkdir(parents=True, exist_ok=True)
                self._pw = sync_playwright().start()
                # A stock Chromium. No stealth flags, no spoofed UA -- if Circle
                # declines to serve a datacenter browser, that is an answer we
                # report, not one we work around.
                self._ctx = self._pw.chromium.launch_persistent_context(
                    str(self.profile_dir),
                    headless=self.headless,
                    viewport=VIEWPORT,
                    args=["--disable-dev-shm-usage", "--no-sandbox"],
                )
                self._page = self._ctx.pages[0] if self._ctx.pages else self._ctx.new_page()
                self.status.started_at = time.time()
            if host:
                self._goto(host)
            else:
                self.status.state = SessionState.AWAITING_LOGIN.value
            return self.status

    def stop(self) -> SessionStatus:
        """Close the browser. The profile (and its login) stays on disk."""
        if self._worker is None:
            self.status = SessionStatus(state=SessionState.STOPPED.value)
            return self.status
        status = self._submit(self._stop_locked, timeout=30)
        with self._lock:
            worker, self._worker = self._worker, None
        if worker is not None:
            worker.shutdown()
        return status

    def _stop_locked(self) -> SessionStatus:
        with self._lock:
            for closer in (
                getattr(self._ctx, "close", None), getattr(self._pw, "stop", None)
            ):
                if closer:
                    try:
                        closer()
                    except Exception:  # noqa: BLE001 - shutdown is best-effort
                        pass
            self._ctx = self._pw = self._page = None
            self.status = SessionStatus(state=SessionState.STOPPED.value)
            return self.status

    @property
    def running(self) -> bool:
        return self._ctx is not None

    # --- navigation + checks ---------------------------------------------

    def _normalize(self, host: str) -> str:
        return host.replace("https://", "").replace("http://", "").strip("/")

    def _goto(self, host: str) -> None:
        """Navigate to a community and classify what came back."""
        host = self._normalize(host)
        self.status.host = host
        page = self._page
        page.goto(f"https://{host}", wait_until="domcontentloaded", timeout=45_000)
        self._classify()

    def _classify(self) -> None:
        """Set the state from what is actually on screen right now."""
        page = self._page
        self.status.last_checked_at = time.time()
        try:
            self.status.current_url = page.url
            html = page.content()
        except Exception as exc:  # noqa: BLE001 - a dead page is an error state
            self.status.state = SessionState.ERROR.value
            self.status.detail = f"{exc.__class__.__name__}: {exc}"
            return

        marker = detect_challenge(page.url, html)
        if marker:
            # Stop here. Do not retry, reload, or attempt to satisfy it.
            self.status.state = SessionState.CHALLENGED.value
            self.status.detail = (
                f"Circle presented a verification challenge ({marker}). "
                "Stopping rather than attempting to bypass it."
            )
            self.status.challenge_url = page.url
            return

        self.status.challenge_url = None
        member = self._current_member()
        if member is not None:
            self.status.state = SessionState.AUTHENTICATED.value
            self.status.member_label = member
            self.status.detail = "Signed in."
        else:
            self.status.state = SessionState.AWAITING_LOGIN.value
            self.status.detail = "Not signed in yet -- log in through the live view."

    def _current_member(self) -> str | None:
        """Return a display label for the signed-in member, else None."""
        page = self._page
        base = f"https://{self.status.host}" if self.status.host else ""
        if not base:
            return None
        for path in _LOGIN_CHECK_PATHS:
            try:
                resp = page.request.get(base + path, headers={"Accept": "application/json"})
            except Exception:
                continue
            if resp.status in (401, 403):
                return None  # definitively signed out
            if resp.status != 200:
                continue
            try:
                data = resp.json()
            except Exception:
                continue
            if isinstance(data, dict):
                body = data.get("community_member") or data.get("user") or data
                label = body.get("name") or body.get("full_name") or body.get("email")
                if label:
                    return str(label)
                return "signed in"
        return None

    # --- interactive view -------------------------------------------------

    def screenshot(self) -> bytes:
        """PNG of the current page, for the dashboard's live view."""
        def _shot():
            self._require_running()
            return self._page.screenshot(type="png")
        return self._submit(_shot)

    def screenshot_b64(self) -> str:
        return base64.b64encode(self.screenshot()).decode("ascii")

    def click(self, x: int, y: int) -> None:
        def _click():
            self._require_running()
            self._page.mouse.click(x, y)
        self._submit(_click)

    def type_text(self, text: str) -> None:
        """Type into the focused field.

        Used for the Circle login form. The text is passed straight to the
        browser and is never logged, stored, or forwarded anywhere.
        """
        def _type():
            self._require_running()
            self._page.keyboard.type(text, delay=25)
        self._submit(_type)

    def press(self, key: str) -> None:
        def _press():
            self._require_running()
            self._page.keyboard.press(key)
        self._submit(_press)

    def navigate(self, host: str) -> SessionStatus:
        def _nav():
            self._require_running()
            self._goto(host)
            return self.status
        return self._submit(_nav)

    def refresh_status(self) -> SessionStatus:
        if not self.running:
            self.status.state = SessionState.STOPPED.value
            return self.status
        def _refresh():
            if not self.running:
                self.status.state = SessionState.STOPPED.value
                return self.status
            self._classify()
            return self.status
        return self._submit(_refresh)

    def _require_running(self) -> None:
        if not self.running:
            raise RemoteBrowserUnavailable("The remote browser is not running.")

    # --- reading ----------------------------------------------------------

    def fetch_json(self, path: str):
        """GET an internal endpoint using the browser's own session.

        Raises ChallengeDetected if Circle answers with a verification wall.
        """
        def _fetch():
            self._require_running()
            base = f"https://{self.status.host}"
            resp = self._page.request.get(base + path, headers={"Accept": "application/json"})
            if resp.status in (401, 403):
                self.status.state = SessionState.AWAITING_LOGIN.value
                return None
            if resp.status == 429 or resp.status == 503:
                body = ""
                try:
                    body = resp.text()
                except Exception:
                    pass
                marker = detect_challenge(base + path, body) or f"HTTP {resp.status}"
                self.status.state = SessionState.CHALLENGED.value
                self.status.detail = f"Circle responded with {marker}. Stopping."
                raise ChallengeDetected(self.status.detail)
            if resp.status >= 400:
                return None
            try:
                return resp.json()
            except Exception:
                return None

        return self._submit(_fetch)
