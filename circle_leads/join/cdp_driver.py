"""Drive the known part of a Circle join from Python, over CDP.

The LLM agent handles a community page for about $0.30-0.90 a visit, and most
of that is spent re-deriving the same five steps: find the login link, fill a
React-controlled field, type six OTP boxes, click the join gate, read the
membership API. Those steps do not change between communities, so they belong
in code; the agent is worth its price only where a page is genuinely new.

This module attaches to a browser that is **already running** with
``--remote-debugging-port`` -- the same one the agent uses -- so both can work
in the same profile and the same Circle session. It never launches or closes
the browser, and never writes to the database.

What it deliberately does NOT do: decide anything about a page it cannot
classify. ``classify`` returns ``UNKNOWN`` and the caller hands that community
to the agent (or to a human), which is the whole point of the split.
"""

from __future__ import annotations

import logging
import re
import time
from contextlib import contextmanager
from dataclasses import dataclass
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

DEFAULT_CDP_URL = "http://127.0.0.1:9222"

# Page states the known path can act on. Anything else is UNKNOWN by design.
MEMBER = "member"
JOIN_GATE = "join_gate"
LOGIN_WALL = "login_wall"
TWO_FA = "two_fa"
PROFILE_STEP = "profile_step"
EXTERNAL_LOGIN = "external_login"
CLOUDFLARE = "cloudflare"
UNKNOWN = "unknown"

_JOIN_TEXTS = ("register for free", "join for free", "join now", "join the community", "join")
_LOGIN_TEXTS = ("log in", "login", "sign in")
_EMAIL_METHOD_TEXTS = ("sign in with an email", "sign in with email", "continue with email", "use email")
_EMAIL_FIELDS = "input[type='email'], input[name='email'], #user_email"

_SPACES_JS = """() => fetch('/internal_api/spaces',
    {headers: {'Accept': 'application/json'}, credentials: 'include'})
  .then(r => r.json())
  .then(d => {
      const a = Array.isArray(d) ? d : (d.records || d.spaces || []);
      return {count: a.length, flags: a.map(s => s.is_space_member)};
  })
  .catch(e => ({error: String(e)}))"""


class DriverError(RuntimeError):
    """The browser is unreachable, or a step was asked for in the wrong state."""


@dataclass
class Membership:
    """What ``/internal_api/spaces`` says about this account, from inside the page."""

    count: int
    flags: list
    error: str | None = None

    @property
    def is_member(self) -> bool:
        """True when at least one space says ``is_space_member``.

        A 200 from the endpoint proves nothing -- open communities serve the
        list anonymously -- and a community can hold spaces the member has not
        opted into, so "all flags true" is too strict. One true flag is the
        signal Circle actually gives us.
        """
        return any(flag is True for flag in self.flags)


def is_circle_host(url: str, community_host: str) -> bool:
    """Whether credentials may be typed on ``url``.

    The rule after the 2026-09-21 incident, when an asset-based check let the
    password reach an auth0 page: only the community's own host, or a
    ``*.circle.so`` host (which includes Circle's central login), counts.
    """
    host = (urlparse(url).hostname or "").lower()
    community_host = (community_host or "").lower().lstrip(".")
    if not host:
        return False
    if community_host and (host == community_host or host == f"www.{community_host}"):
        return True
    return host == "circle.so" or host.endswith(".circle.so")


@contextmanager
def attach(cdp_url: str = DEFAULT_CDP_URL):
    """Yield the first page of an already-running browser; never closes it."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        try:
            browser = p.chromium.connect_over_cdp(cdp_url)
        except Exception as exc:  # playwright raises its own Error type
            raise DriverError(f"no browser on {cdp_url}: {exc}") from exc
        try:
            context = browser.contexts[0] if browser.contexts else browser.new_context()
            page = context.pages[0] if context.pages else context.new_page()
            yield page
        finally:
            # Detach only. Closing would take the agent's browser down with us.
            browser.close()


def classify(page) -> str:
    """Name the state of the page currently open, without touching it."""
    url = (page.url or "").lower()
    if "/two_fa" in url:
        return TWO_FA
    if "/settings/profile" in url and "new_state=true" in url:
        return PROFILE_STEP

    try:
        text = (page.inner_text("body") or "").lower()
    except Exception as exc:  # a page mid-navigation has no body yet
        logger.debug("no body text on %s: %s", url, exc)
        return UNKNOWN

    if "just a moment" in text or "checking your browser" in text:
        return CLOUDFLARE
    if any(t in text for t in ("members only", "this community is open for registered members")):
        return JOIN_GATE if _has_text_button(page, _JOIN_TEXTS) else LOGIN_WALL
    if _has_text_button(page, _JOIN_TEXTS):
        return JOIN_GATE
    if _has_text_button(page, _LOGIN_TEXTS):
        return LOGIN_WALL
    return UNKNOWN


def assess(page) -> tuple[str, Membership]:
    """The state to act on, with membership taking precedence over looks.

    Measured 2026-09-25: a community we had already joined still rendered a
    "Join" button, and a member view with no buttons at all classified as
    UNKNOWN. Both would have sent a community we are already in to the agent.
    The API answer decides; the DOM only matters when we are not a member.
    """
    member = membership(page)
    if member.is_member:
        return MEMBER, member
    return classify(page), member


def _has_text_button(page, texts: tuple[str, ...]) -> bool:
    try:
        labels = page.eval_on_selector_all(
            "a, button", "els => els.map(e => (e.textContent || '').trim().toLowerCase())"
        )
    except Exception:
        return False
    return any(any(t == label or t in label for t in texts) for label in labels)


def membership(page) -> Membership:
    """Ask the community's own API whether this account is a member."""
    try:
        result = page.evaluate(_SPACES_JS)
    except Exception as exc:
        raise DriverError(f"membership check failed on {page.url}: {exc}") from exc
    if isinstance(result, dict) and result.get("error"):
        return Membership(count=0, flags=[], error=str(result["error"]))
    return Membership(count=int(result.get("count", 0)), flags=list(result.get("flags") or []))


def sign_in(page, *, email: str, password: str, community_host: str, timeout_ms: int = 15000) -> str:
    """Sign in on the page that is open. Returns the state afterwards.

    Circle's central login (``login.circle.so``) asks for the email first and
    only then renders the password field, so the password is typed after that
    field exists -- filling early silently fills nothing.

    Inputs are React-controlled: assigning ``value`` does not register, so the
    text goes in through the keyboard, which is what the page's own handlers
    listen to.
    """
    if not is_circle_host(page.url, community_host):
        logger.warning("refusing to type credentials on %s", page.url)
        return EXTERNAL_LOGIN

    # A community's home page carries no login form; Circle serves it at
    # /users/sign_in, which then redirects to the central login when the
    # community uses one. Measured on generouslifeapp, which greets a visitor
    # with a join gate and no field to type into.
    if page.query_selector(_EMAIL_FIELDS) is None:
        page.goto(f"https://{community_host}/users/sign_in", wait_until="domcontentloaded")
        page.wait_for_timeout(1000)
        if not is_circle_host(page.url, community_host):
            return EXTERNAL_LOGIN
        # The form is rendered by the app, not served in the HTML: on the
        # droplet it was still missing a second after the navigation and an
        # immediate query gave up on a page that was about to be fine.
        try:
            page.wait_for_selector(_EMAIL_FIELDS, timeout=timeout_ms)
        except Exception:
            # Circle's sign-in page can open on a choice of method with no
            # fields at all ("Log in to your account / Sign in with an email").
            # Measured on thefpahub from the droplet: zero <input> elements
            # until that button is clicked.
            if not _click_text(page, _EMAIL_METHOD_TEXTS):
                return UNKNOWN
            page.wait_for_timeout(1500)
            try:
                page.wait_for_selector(_EMAIL_FIELDS, timeout=timeout_ms)
            except Exception:
                return UNKNOWN

    _type_into(page, _EMAIL_FIELDS, email)
    _submit(page, ("sign in", "continue", "log in", "next"))
    page.wait_for_timeout(1500)

    if not is_circle_host(page.url, community_host):
        return EXTERNAL_LOGIN
    try:
        page.wait_for_selector("input[type='password']", timeout=timeout_ms)
    except Exception:
        # Some communities sign in on the email step alone (magic link, SSO).
        return classify(page)

    _type_into(page, "input[type='password']", password)
    _submit(page, ("sign in", "log in", "continue"))
    # Circle finishes the login asynchronously: measured on the droplet, the
    # page was still the sign-in form when networkidle returned and only then
    # swapped to /feed. Reporting a login_wall there costs a whole visit, so
    # wait for the URL to actually leave the form.
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        page.wait_for_timeout(1500)
        if "sign_in" not in (page.url or ""):
            break
    return classify(page)


def enter_code(page, code: str, timeout_ms: int = 15000) -> str:
    """Type the emailed 2FA code. Returns the state afterwards.

    The code sits in six single-digit ``input[type=tel]`` boxes. Clicking the
    first one and inserting all six characters lets Circle's own handler move
    between boxes and auto-submit -- filling the boxes one by one from JS does
    not register at all.
    """
    if not re.fullmatch(r"\d{6}", code or ""):
        raise DriverError(f"not a 6-digit code: {code!r}")
    boxes = page.query_selector_all("input[type='tel'], input[inputmode='numeric']")
    if not boxes:
        raise DriverError(f"no code inputs on {page.url}")
    boxes[0].click()
    page.keyboard.insert_text(code)
    page.wait_for_timeout(2000)
    try:
        page.wait_for_load_state("networkidle", timeout=timeout_ms)
    except Exception:
        pass
    return classify(page)


def click_join(page, timeout_ms: int = 15000) -> str:
    """Click the community's join gate. Returns the state afterwards."""
    for label in _JOIN_TEXTS:
        button = page.query_selector(f"button:has-text('{label}'), a:has-text('{label}')")
        if button:
            button.click()
            page.wait_for_timeout(2000)
            try:
                page.wait_for_load_state("networkidle", timeout=timeout_ms)
            except Exception:
                pass
            return classify(page)
    return UNKNOWN


def _click_text(page, texts: tuple[str, ...]) -> bool:
    """Click the first link or button whose label contains one of ``texts``."""
    for text in texts:
        element = page.query_selector(f"button:has-text('{text}'), a:has-text('{text}')")
        if element:
            element.click()
            return True
    return False


def _type_into(page, selector: str, value: str) -> None:
    element = page.query_selector(selector)
    if element is None:
        raise DriverError(f"no field {selector!r} on {page.url}")
    element.click()
    page.keyboard.insert_text(value)


def _submit(page, labels: tuple[str, ...]) -> None:
    for label in labels:
        button = page.query_selector(f"button:has-text('{label}'), input[type=submit][value*='{label}' i]")
        if button:
            button.click()
            return
    page.keyboard.press("Enter")
