"""Join one community in the droplet's own browser, in one visit.

The Ego driver (``ego_join_driver.mjs``) runs inside a Mac-only browser and
stops at the two steps it cannot do alone: the six-digit code Circle emails,
and the answers to a community's required profile questions. The code went to
a person; the answers went back to Python for a second visit. Here the browser
is ``warmr-browser.service`` on the droplet (Chrome with CDP on localhost), so
the walk runs in the process that can also read the bot's mailbox and the
answer database: the code and the answers are typed the moment the page asks.

The walk itself is the Ego driver's, ported rule for rule, because every rule
there was measured live:

- credentials are typed only on the community's own host or ``*.circle.so``
  (the 2026-09-21 auth0 incident);
- membership is Circle's own answer (``/internal_api/pundit_users``,
  ``/internal_api/spaces``), never how the page looks;
- a Cloudflare challenge stops the visit, no bypass is attempted;
- a checkout URL means paid; a form asking more than an email and a password
  goes to a person.

The result has the Ego driver's shape (``EgoJoinResult``), so the batch in
``joiner.py`` records both the same way. A walk never raises for a page-level
surprise: it answers ``driver_error`` and the batch moves on.
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from circle_leads.join.cdp_driver import is_circle_host
from circle_leads.join.ego_bridge import EgoJoinResult
from circle_leads.join.forms import FormField

logger = logging.getLogger(__name__)

# --- words and selectors: kept in step with ego_join_driver.mjs -------------------
# (tests/test_cdp_join.py fails if the two lists drift apart)

JOIN_TEXTS = [
    "Join", "Join for free", "Join now", "Join community", "Sign up", "Sign up for free", "Get access",
    "Request to join", "Accept invitation", "Register for Free", "Register",
    "Join space",
    "Buy",
    "Rejoindre", "Rejoindre la communauté", "S'inscrire", "Demander à rejoindre",
    "Beitreten", "Jetzt beitreten", "Registrieren", "Mitglied werden",
    "Unirse", "Únete", "Unirme", "Registrarse", "Solicitar unirse",
    "Participar", "Inscrever-se", "Cadastre-se", "Entrar na comunidade",
    "Unisciti", "Iscriviti", "Dołącz", "Zarejestruj się",
    "Word lid", "Lid worden", "Registreren",
]
LOGIN_TEXTS = [
    "Log in", "Sign in", "Login",
    "Se connecter", "Connexion", "Anmelden", "Einloggen", "Iniciar sesión",
    "Iniciar sessão", "Entrar", "Accedi", "Zaloguj się", "Inloggen",
]
LOGIN_SUBMIT_TEXTS = [
    *LOGIN_TEXTS, "Continue", "Submit",
    "Continuer", "Weiter", "Continuar", "Continua", "Dalej", "Doorgaan",
]
EMAIL_STEP_TEXTS = [
    "Sign in", "Log in", "Continue", "Next",
    "Se connecter", "Continuer", "Suivant", "Anmelden", "Weiter",
    "Iniciar sesión", "Continuar", "Siguiente", "Accedi", "Continua", "Avanti",
    "Zaloguj się", "Dalej", "Inloggen", "Doorgaan",
]
LOGIN_PIVOT_TEXTS = [
    "Sign in", "Log in", "Sign in with an email", "Continue with email", "Log in with email",
    "Se connecter", "Anmelden", "Iniciar sesión", "Accedi", "Zaloguj się", "Inloggen",
]
PROFILE_CONTINUE_TEXTS = [
    "Continue", "Save", "Save and continue", "Next", "Done", "Finish",
    "Continuer", "Enregistrer", "Suivant", "Terminer",
    "Weiter", "Speichern", "Fertig", "Continuar", "Guardar", "Siguiente",
    "Continua", "Salva", "Avanti", "Dalej", "Zapisz", "Doorgaan", "Opslaan",
]
SIGNUP_SUBMIT_TEXTS = ["Sign up", "Create account", "Register", "Join", "Continue", "Submit"]
EMAIL_SELECTOR = (
    "input[type=email], input[name*=email i], input[autocomplete=email], input[autocomplete=username]"
)
PASSWORD_SELECTOR = "input[type=password]"
SESSION_COOKIE_NAMES = ["_circle_session", "remember_user_token", "user_session_identifier"]

# The emailed code. Circle shows it as six single-digit boxes (measured on
# /two_fa and on the join step, 2026-09-25): clicking the first box and
# inserting all six characters lets Circle's own handler fill and submit them.
CODE_INPUTS = "input[type=tel], input[inputmode=numeric], input[autocomplete=one-time-code]"
CODE_SUBMIT_TEXTS = ["Verify", "Continue", "Submit", "Confirm", "Log in", "Sign in", "Next"]
# A second code in one visit is normal (sign-in, then the join step); a third
# means the page is refusing what we type.
MAX_CODES_PER_VISIT = 2

# --- page scripts ------------------------------------------------------------------
# Module constants, not inline strings: the tests' fake page answers by
# identity, so what the walk asks the page stays visible in one place.

FETCH_JS = """async (path) => {
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), 15000);
  try {
    const r = await fetch(path, {credentials: 'include', headers: {Accept: 'application/json'},
                                 signal: ctl.signal});
    return {status: r.status, body: await r.text()};
  } catch (e) {
    return {status: 0, body: ''};
  } finally {
    clearTimeout(timer);
  }
}"""

SETTLE_JS = """() => {
  const text = (document.body && document.body.innerText) || '';
  return {
    ready: document.readyState,
    len: text.trim().length,
    nodes: document.getElementsByTagName('*').length,
    busy: !!document.querySelector('[aria-busy="true"], [role="progressbar"]'),
  };
}"""

PAGE_STATE_JS = """({joinTexts, loginTexts}) => {
  const norm = (t) => (t || '').replace(/\\s+/g, ' ').trim().toLowerCase();
  const joinSet = new Set(joinTexts.map(norm));
  const loginSet = new Set(loginTexts.map(norm));
  const clickable = [...document.querySelectorAll('button, a, [role=button]')]
    .map((el) => norm(el.textContent)).filter(Boolean);
  const bodyText = document.body.innerText || '';
  return {
    inviteOnlyGate: /this is a private community/i.test(bodyText) &&
      /doesn.?t look like you have access|admin may need to add you/i.test(bodyText),
    hasLogin: clickable.some((t) => loginSet.has(t)),
    hasJoin: clickable.some((t) => joinSet.has(t)),
    onCheckout: /\\/checkout(\\/|$)/.test(location.pathname),
    subscriptionExpired: /\\/subscription_expired(\\/|$)/.test(location.pathname),
    newMemberOnboarding: /\\/settings\\/profile/.test(location.pathname) && /new_state=true/.test(location.search),
    challenge: /verifying you are human|checking your browser|attention required|just a moment/i.test(bodyText),
    url: location.href,
  };
}"""

POST_CLICK_JS = """() => {
  const bodyText = document.body.innerText || '';
  const formQuestions = [...document.querySelectorAll('form label, form textarea, form input[type=text]')]
    .map((el) => (el.closest('label')?.textContent || el.getAttribute('placeholder') ||
                  el.getAttribute('aria-label') || '').trim())
    .filter(Boolean);
  return {
    pending: /pending approval|awaiting approval|request (has been )?(received|submitted)|thank you for (applying|your interest)/i.test(bodyText),
    formQuestions: [...new Set(formQuestions)],
    onCheckout: /\\/checkout/.test(location.pathname),
    newMemberOnboarding: /\\/settings\\/profile/.test(location.pathname) && /new_state=true/.test(location.search),
    url: location.href,
  };
}"""

MARK_CLICK_JS = """(wanted) => {
  const norm = (t) => (t || '').replace(/\\s+/g, ' ').trim().toLowerCase();
  document.querySelectorAll('[data-warmr-click]').forEach((el) => el.removeAttribute('data-warmr-click'));
  const visible = (el) => !!(el.offsetParent || el.getClientRects().length) && !el.disabled;
  const candidates = [...document.querySelectorAll('button, a, [role=button], input[type=submit]')].filter(visible);
  for (const text of wanted) {
    const t = norm(text);
    const el = candidates.find((c) => norm(c.textContent || c.value) === t);
    if (el) { el.setAttribute('data-warmr-click', '1'); return text; }
  }
  return null;
}"""

FIELDS_JS = """({emailSelector, passwordSelector}) => ({
  hasEmail: !!document.querySelector(emailSelector),
  hasPassword: !!document.querySelector(passwordSelector),
})"""

SIGNUP_SHAPE_JS = """({emailSelector}) => {
  const form = [...document.querySelectorAll('form')].find((f) => f.querySelector('input[type=password]'));
  if (!form) return {fillable: false, reason: 'no form holds a password field'};
  const visible = (el) => {
    const style = window.getComputedStyle(el);
    return style.display !== 'none' && style.visibility !== 'hidden' && el.type !== 'hidden';
  };
  const kinds = [...form.querySelectorAll('input, textarea, select')]
    .filter(visible)
    .filter((el) => !['submit', 'button', 'image', 'reset'].includes(el.type))
    .map((el) => {
      if (el.tagName !== 'INPUT') return el.tagName.toLowerCase();
      if (el.type === 'password') return 'password';
      if (el.matches(emailSelector)) return 'email';
      return (el.type || 'text').toLowerCase();
    });
  const asked = [...new Set(kinds.filter((k) => !['email', 'password', 'checkbox'].includes(k)))];
  return {
    fillable: asked.length === 0 && kinds.includes('password'),
    hasEmail: kinds.includes('email'),
    reason: asked.length ? `the form also asks for ${asked.join(', ')}` : '',
  };
}"""

SPACE_LINKS_JS = """() => [...new Set([...document.querySelectorAll('a[href*="/c/"]')]
  .map((a) => { try { return new URL(a.href, location.href); } catch (e) { return null; } })
  .filter((u) => u && u.origin === location.origin)
  .map((u) => u.pathname))]"""

CODE_PAGE_JS = """(inputs) => {
  const text = (document.body && document.body.innerText) || '';
  if (/\\/two_fa/.test(location.pathname)) return true;
  return /check your inbox|verification code|enter the code we sent/i.test(text) &&
    document.querySelectorAll(inputs).length > 0;
}"""

PROFILE_FIELD_ELEMENTS_JS = """(prefix) => [...document.querySelectorAll('[name^="' + prefix + '"]')]
  .map((e) => ({
    tag: e.tagName.toLowerCase(),
    type: (e.getAttribute('type') || '').toLowerCase(),
    name: e.getAttribute('name'),
    value: e.getAttribute('value'),
    label: (e.closest('label')?.textContent || '').trim(),
    visible: !!(e.offsetParent || e.getClientRects().length),
  }))"""

CHECKED_JS = """(name) => !!document.querySelector('[name="' + name + '"]').checked"""
SELECTED_JS = """(name) => [...document.querySelector('[name="' + name + '"]').selectedOptions]
  .map((o) => o.textContent.trim())"""

_TRANSIENT = re.compile(
    r"reading 'innerText'|reading 'body'|Cannot read properties of null|"
    r"Execution context was destroyed|because of a navigation",
    re.IGNORECASE,
)
_NOT_A_PAGE = ("about:blank", "chrome-error:")


class _ForeignLogin(Exception):
    """The credential form is on a host that is not Circle."""


def _has_value(cmpf: Any) -> bool:
    """Whether a member's answer to a profile field is already there."""
    if not isinstance(cmpf, dict):
        return False
    for key, value in cmpf.items():
        if key in ("id", "created_at", "updated_at", "profile_field_id", "community_member_id"):
            continue
        if value is None or value == "" or value is False:
            continue
        if isinstance(value, (list, dict)) and not value:
            continue
        return True
    return False


def _choice_text(choice: Any) -> str:
    if isinstance(choice, dict):
        for key in ("value", "label", "name", "text"):
            if choice.get(key) is not None:
                return str(choice[key])
        return ""
    return str(choice)


class _Walk:
    """One visit to one community. Every method answers what the page did."""

    def __init__(self, page, url: str, *, email: str | None, password: str | None,
                 fetch_code: Callable[[datetime], str | None] | None,
                 answer_profile: Callable[[list[FormField]], Any] | None,
                 screenshot_dir: str | None, now: Callable[[], datetime]):
        self.page = page
        self.url = url
        self.community_host = (urlparse(url).hostname or "").lower()
        self.email = email
        self.password = password
        self.fetch_code = fetch_code
        self.answer_profile = answer_profile
        self.screenshot_dir = screenshot_dir
        self.now = now
        # A code for this visit is mailed after it began; an older one belongs
        # to another visit and would only be refused.
        self.code_since = now() - timedelta(seconds=5)
        self.codes_typed = 0
        self.profile_step_done = False

    # --- results -----------------------------------------------------------------

    def _shot(self, tag: str) -> str | None:
        if not self.screenshot_dir:
            return None
        try:
            path = Path(self.screenshot_dir) / f"{tag}-{int(time.time() * 1000)}.png"
            self.page.screenshot(path=str(path))
            return str(path)
        except Exception:  # noqa: BLE001 - a screenshot must never change the outcome
            return None

    def _done(self, status: str, detail: str, *, cookies: list | None = None,
              tag: str | None = None) -> EgoJoinResult:
        return EgoJoinResult(status=status, detail=detail, screenshot=self._shot(tag or status),
                             cookies=cookies)

    # --- the page ------------------------------------------------------------------

    def _url(self) -> str:
        try:
            return str(self.page.url or "")
        except Exception:  # noqa: BLE001
            return ""

    def _eval(self, script: str, arg: Any = None) -> Any:
        """Evaluate, riding out a page that is mid-navigation."""
        for delay_ms in (800, 1600, 2400):
            try:
                return self.page.evaluate(script) if arg is None else self.page.evaluate(script, arg)
            except Exception as exc:  # noqa: BLE001 - Playwright raises its own Error
                if not _TRANSIENT.search(str(exc)):
                    raise
                self.page.wait_for_timeout(delay_ms)
        return self.page.evaluate(script) if arg is None else self.page.evaluate(script, arg)

    def _fetch_json(self, path: str) -> tuple[int, Any]:
        try:
            raw = self._eval(FETCH_JS, path) or {}
        except Exception:  # noqa: BLE001
            return 0, None
        try:
            return int(raw.get("status") or 0), json.loads(raw.get("body") or "")
        except ValueError:
            return int(raw.get("status") or 0), None

    def _settle(self, max_ms: int = 15000) -> bool:
        """Wait until a client-rendered page has rendered: network quiet, some
        text, nothing busy, and the DOM unchanged between two looks."""
        deadline = time.monotonic() + max_ms / 1000
        try:
            self.page.wait_for_load_state("networkidle", timeout=max_ms)
        except Exception:  # noqa: BLE001 - a chatty page never goes idle
            pass
        last = None
        while time.monotonic() < deadline:
            try:
                snap = self.page.evaluate(SETTLE_JS)
            except Exception:  # noqa: BLE001 - mid-navigation
                snap = None
            if (snap and last and snap.get("ready") == "complete" and snap.get("len", 0) > 40
                    and not snap.get("busy") and snap.get("len") == last.get("len")
                    and snap.get("nodes") == last.get("nodes")):
                return True
            last = snap
            self.page.wait_for_timeout(600)
        return False

    def _navigate(self, target: str) -> dict | None:
        """Open ``target``; a dict says why it could not be opened."""
        for attempt in range(2):
            try:
                self.page.goto(target, timeout=40_000)
                return None
            except Exception as exc:  # noqa: BLE001
                message = str(exc)
                code_match = re.search(r"net::[A-Z_]+", message)
                code = code_match.group(0) if code_match else message[:120]
                now = self._url()
                if "ERR_NAME_NOT_RESOLVED" in message:
                    where = (f" (redirect ended at {now})"
                             if now and not now.startswith(_NOT_A_PAGE) and now != target else "")
                    return {"status": "dead_host",
                            "detail": f"Host no longer resolves: {target}{where} -- {code}."}
                if "ERR_ABORTED" in message:
                    self.page.wait_for_timeout(3000)
                    after = self._url()
                    if after and not after.startswith(_NOT_A_PAGE):
                        return None
                    continue
                if attempt == 0 and re.search(r"timeout|timed out|ERR_TIMED_OUT|ERR_CONNECTION",
                                              message, re.IGNORECASE):
                    continue
                return {"status": "nav_failed", "detail": f"Could not open {target}: {code}."}
        return {"status": "nav_failed",
                "detail": f"Could not open {target}: the load kept aborting after a retry."}

    def _origin(self) -> str:
        parsed = urlparse(self._url() or self.url)
        return f"{parsed.scheme}://{parsed.netloc}"

    def _page_state(self) -> dict:
        return self._eval(PAGE_STATE_JS, {"joinTexts": JOIN_TEXTS, "loginTexts": LOGIN_TEXTS})

    def _post_click_state(self) -> dict:
        return self._eval(POST_CLICK_JS)

    def _click_first(self, texts: list[str]) -> str | None:
        hit = self._eval(MARK_CLICK_JS, texts)
        if not hit:
            return None
        try:
            self.page.click('[data-warmr-click="1"]')
            return hit
        except Exception:  # noqa: BLE001
            return None

    def _member_state(self) -> dict:
        """Who Circle says we are here. api=False: not a Circle page at all."""
        status, data = self._fetch_json("/internal_api/pundit_users")
        if status != 200 or not isinstance(data, dict) or "current_community" not in data:
            return {"api": False, "status": status}
        user = data.get("current_user") or None
        member = data.get("current_community_member") or None
        email = str((user or {}).get("email") or "").lower()
        expected = (self.email or "").lower()
        return {
            "api": True,
            "logged_in": bool(user),
            "other_account": bool(user) and bool(expected) and bool(email) and email != expected,
            "is_member": bool(member),
        }

    def _api_readable(self) -> dict:
        """Can this session read the community? 400 "Please confirm before
        proceeding" means the new-member profile step is still open."""
        status, data = self._fetch_json("/internal_api/spaces")
        message = str(data.get("message") or "") if isinstance(data, dict) else ""
        return {
            "ok": status == 200 and not (isinstance(data, dict) and data.get("success") is False),
            "status": status,
            "profile_pending": status == 400 and "please confirm" in message.lower(),
            "message": message,
        }

    def _session_cookies(self) -> list[dict]:
        """The member session for the page's own host, shaped for
        replay_store.connect_host. The domain is the host the page ended on."""
        here = self._url()
        host = (urlparse(here).hostname or self.community_host).lower()
        try:
            jar = self.page.context.cookies(here)
        except Exception:  # noqa: BLE001
            return []
        return [{"name": c["name"], "value": c["value"], "domain": host}
                for c in jar if c.get("name") in SESSION_COOKIE_NAMES]

    # --- credentials ------------------------------------------------------------------

    def _fill_credential(self, selector: str, value: str) -> None:
        """The only place credentials are typed. Refuses unless the page is Circle.

        Circle sends ``<slug>.circle.so`` on to the community's custom domain,
        so the form can sit on a host that is not the one we opened. That host
        counts only when Circle's own API answers on it
        (``/internal_api/pundit_users`` with ``current_community``) -- the
        auth0 page that took the password on 2026-09-21 answers nothing of
        the sort, and neither does a community's marketing site.
        """
        here = self._url()
        host = (urlparse(here).hostname or "").lower()
        if not is_circle_host(here, self.community_host):
            if not host or not self._member_state()["api"]:
                raise _ForeignLogin(host or "an unknown page")
            logger.info("following Circle's redirect to its own domain %s", host)
            self.community_host = host
        # Circle's fields are React-controlled; fill() goes through input
        # events, which React listens to.
        self.page.fill(selector, value)

    def _attempt_login(self, *, on_login_page: bool) -> dict:
        """Click Log in, then fill whatever email/password form appears -- or
        notice that Circle signed us in silently. Never guesses past an
        unrecognized shape."""
        if not on_login_page and not self._click_first(LOGIN_TEXTS):
            return {"ok": False, "reason": "could not click a Log in/Sign in control"}

        def fields() -> dict:
            try:
                return self._eval(FIELDS_JS, {"emailSelector": EMAIL_SELECTOR,
                                              "passwordSelector": PASSWORD_SELECTOR})
            except Exception:  # noqa: BLE001
                return {"hasEmail": False, "hasPassword": False}

        saw_form = False
        for _ in range(8):
            self.page.wait_for_timeout(800)
            ms = self._member_state()
            if ms["api"] and ms.get("logged_in"):
                return {"ok": True}  # resolved silently, no form shown
            f = fields()
            if f.get("hasEmail") or f.get("hasPassword"):
                saw_form = True
                break
        if not saw_form:
            return {"ok": False,
                    "reason": "clicking Log in neither signed in nor showed a form within 6.4s"}

        for _ in range(4):
            f = fields()
            try:
                if f.get("hasPassword"):
                    if f.get("hasEmail"):
                        self._fill_credential(EMAIL_SELECTOR, self.email or "")
                    self._fill_credential(PASSWORD_SELECTOR, self.password or "")
                    if not self._click_first(LOGIN_SUBMIT_TEXTS):
                        return {"ok": False, "reason": "filled credentials but found no submit control"}
                    self.page.wait_for_timeout(2000)
                    self._settle(12000)
                    return {"ok": True}
                if f.get("hasEmail"):
                    self._fill_credential(EMAIL_SELECTOR, self.email or "")
                    # "Sign in" before "Continue": an email-first form that
                    # defaults to *creating* an account shows "Sign up" with a
                    # "Sign in" link beside it. Never click "Sign up" here.
                    if not self._click_first(EMAIL_STEP_TEXTS):
                        return {"ok": False, "reason": "filled email but found no Sign in/Continue "
                                                       "control before a password step"}
                    self.page.wait_for_timeout(1500)
                    continue
            except _ForeignLogin as exc:
                return {"ok": False, "foreign": str(exc)}
            if not self._click_first(LOGIN_PIVOT_TEXTS):
                return {"ok": False, "reason": "no recognizable email/password field, and no known "
                                               "pivot control, after clicking Log in"}
            self.page.wait_for_timeout(1500)
        return {"ok": False, "reason": "no email/password form reached after 4 navigation hops"}

    # --- the emailed code ---------------------------------------------------------------

    def _on_code_page(self) -> bool:
        try:
            return bool(self._eval(CODE_PAGE_JS, CODE_INPUTS))
        except Exception:  # noqa: BLE001
            return False

    def _code_step(self, where: str, *, member: bool) -> EgoJoinResult | None:
        """Type the code Circle just mailed. None when the page moved past it.

        ``member``: the code is asked after the join itself -- the membership
        is there and only this account can finish it (``email_code_needed``,
        recorded as profile_pending). Before that it is a login that did not
        complete (``login_code_needed``, a handoff).
        """
        status = "email_code_needed" if member else "login_code_needed"
        if self.fetch_code is None:
            return self._done(status,
                              f"Circle asks for the emailed code {where}, and this run has no "
                              "mailbox to read it from.")
        if self.codes_typed >= MAX_CODES_PER_VISIT:
            return self._done(status,
                              f"Circle asks for an emailed code {where} again after "
                              f"{self.codes_typed} were typed in this visit -- stopped.")
        try:
            code = self.fetch_code(self.code_since)
        except Exception as exc:  # noqa: BLE001 - EmailCodeError and IMAP trouble alike
            return self._done(status,
                              f"Circle asks for the emailed code {where}; the mailbox could not be "
                              f"read ({type(exc).__name__}: {str(exc)[:200]}).")
        if not code:
            return self._done(status,
                              f"Circle asks for the emailed code {where}; none arrived in the "
                              "bot's mailbox within the wait.")
        boxes = self.page.query_selector_all(CODE_INPUTS)
        if not boxes:
            return self._done(status,
                              f"Got the emailed code {where}, but the page shows no field for it "
                              f"({self._url()}).")
        boxes[0].click()
        self.page.keyboard.insert_text(code)
        self.codes_typed += 1
        self.code_since = self.now()  # a later code must be newer than this one
        self.page.wait_for_timeout(2500)
        if self._on_code_page():
            self._click_first(CODE_SUBMIT_TEXTS)
            self.page.wait_for_timeout(2500)
        self._settle(12000)
        if self._on_code_page():
            return self._done(status,
                              f"Typed the emailed code {where}; Circle still asks for it "
                              "(expired, or a newer one was sent).", tag="code-refused")
        return None

    # --- sign in -------------------------------------------------------------------------

    def _sign_in(self, *, has_login_control: bool = True) -> EgoJoinResult | None:
        """Sign in as this account. None when signed in; a result when stopped."""
        if not (self.email and self.password):
            return self._done("needs_login", "Not signed in and no credentials configured.")
        on_login_page = False
        if not has_login_control:
            nav = self._navigate(f"{self._origin()}/users/sign_in")
            if nav:
                return self._done("needs_login", f"Signed out, no Log in control, and "
                                                 f"{self._origin()}/users/sign_in did not open: "
                                                 f"{nav['detail']}")
            self._settle()
            on_login_page = True
        login = self._attempt_login(on_login_page=on_login_page)
        if login.get("foreign"):
            return self._done(
                "external_login",
                f"The community signs in through its own site ({login['foreign']}), not Circle "
                "-- it needs an account there. The bot never types the Circle login outside Circle.",
            )
        if not login.get("ok"):
            return self._done("needs_login", f"Automatic login failed ({login.get('reason')}).",
                              tag="login-failed")
        self._settle()
        if self._on_code_page():
            stopped = self._code_step("at sign-in", member=False)
            if stopped:
                return stopped
        state = self._page_state()
        if state.get("challenge"):
            return self._done("challenge_stop", "A challenge appeared right after login -- stopped, "
                                                "no bypass attempted.", tag="challenge-after-login")
        ms = self._member_state()
        if ms.get("other_account"):
            return self._done("wrong_account",
                              "After login a different Circle account is signed in -- not joining as it.")
        if (not ms.get("logged_in")) if ms["api"] else state.get("hasLogin"):
            return self._done("needs_login", "Submitted login but still signed out (wrong password "
                                             "on this host?).", tag="still-logged-out")
        return None

    # --- the profile step -----------------------------------------------------------------

    def _read_profile_form(self) -> dict | None:
        """Circle's own description of "Create a profile": name, time zone and
        profile_fields[] -- their index is the index in the form's input names."""
        status, data = self._fetch_json("/internal_api/signup/profile")
        if status != 200 or not isinstance(data, dict) or not isinstance(data.get("profile_fields"), list):
            return None
        missing: list[tuple[int, FormField]] = []
        for index, raw in enumerate(data["profile_fields"]):
            if not isinstance(raw, dict) or not raw.get("required"):
                continue
            if _has_value(raw.get("community_member_profile_field")):
                continue
            missing.append((index, FormField(
                id=str(raw.get("id")),
                label=str(raw.get("label") or raw.get("key") or f"field {index}"),
                field_type=str(raw.get("field_type") or "text").strip().lower(),
                required=True,
                choices=[t for t in (_choice_text(c) for c in (raw.get("choices") or [])) if t.strip()],
                description=raw.get("description") or None,
                platform_field=bool(raw.get("platform_field")),
            )))
        return {
            "has_name": bool(str(data.get("name") or "").strip()),
            "has_time_zone": bool(str(data.get("time_zone") or "").strip()),
            "missing": missing,
        }

    def _fill_profile_field(self, index: int, value: Any) -> bool:
        """Put one answer into field ``index``. Plain inputs, textareas, native
        selects, checkboxes and radios only -- a custom widget goes to a person."""
        prefix = f"community_member_profile_fields_attributes.{index}."
        elements = [e for e in (self._eval(PROFILE_FIELD_ELEMENTS_JS, prefix) or []) if e.get("visible")]

        def css(name: str) -> str:
            return f'[name="{name}"]'

        if isinstance(value, bool):
            box = next((e for e in elements if e.get("type") == "checkbox"), None)
            if box is None:
                return False
            if bool(self._eval(CHECKED_JS, box["name"])) != value:
                self.page.click(css(box["name"]))
            return True
        select = next((e for e in elements if e.get("tag") == "select"), None)
        if select is not None:
            wanted = [str(v) for v in (value if isinstance(value, list) else [value])]
            self.page.select_option(css(select["name"]), wanted if isinstance(value, list) else wanted[0])
            shown = self._eval(SELECTED_JS, select["name"]) or []
            return all(w in shown for w in wanted)
        radios = [e for e in elements if e.get("type") == "radio"]
        if radios:
            target = next((r for r in radios if r.get("label") == str(value)
                           or r.get("value") == str(value)), None)
            if target is None:
                return False
            self.page.click(f'[name="{target["name"]}"][value="{target["value"]}"]')
            return True
        box = next((e for e in elements if e.get("tag") == "textarea" or (
            e.get("tag") == "input" and e.get("type") in ("", "text", "url", "number", "email", "tel"))),
            None)
        if box is not None and not isinstance(value, list):
            self.page.fill(css(box["name"]), str(value))
            return True
        return False

    def _complete_profile_step(self) -> EgoJoinResult | None:
        """One pass over "Create a profile". None to look again; a result when stopped."""
        if self._on_code_page():
            return self._code_step("on the profile step", member=True)
        if "/settings/profile" not in self._url():
            nav = self._navigate(f"{self._origin()}/settings/profile?new_state=true")
            if nav:
                return self._done("profile_incomplete",
                                  f"Member, but the profile page did not open: {nav['detail']}")
            self._settle()
        form = self._read_profile_form()
        if form is None:
            return self._done("profile_incomplete", "Member, but Circle did not describe the profile "
                                                    "form (/internal_api/signup/profile).")
        if not form["has_name"] or not form["has_time_zone"]:
            empty = "Full name" if not form["has_name"] else "Timezone"
            return self._done("profile_incomplete",
                              f"Profile step: {empty} is empty and is not something the bot fills.")
        if form["missing"]:
            fields = [f for _, f in form["missing"]]
            resolution = self.answer_profile(fields) if self.answer_profile else None
            answers = dict(getattr(resolution, "answers", None) or {})
            unanswered = [f for f in fields if answers.get(f.id) is None]
            if resolution is None or unanswered:
                labels = unanswered or fields
                return self._done("profile_incomplete",
                                  "Required profile question(s) the bot may not answer: "
                                  + ", ".join(f'"{f.label}"' for f in labels)
                                  + " -- answer them in join_form_answers, then run again.")
            for index, f in form["missing"]:
                try:
                    filled = self._fill_profile_field(index, answers[f.id])
                except Exception:  # noqa: BLE001
                    filled = False
                if not filled:
                    return self._done("profile_incomplete",
                                      f'Profile step: could not fill required "{f.label}" '
                                      f"({f.field_type}) -- custom widget?")
        if not self._click_first(PROFILE_CONTINUE_TEXTS):
            if self._on_code_page():
                return self._code_step("on the profile step", member=True)
            return self._done("profile_incomplete", "Profile step: no Continue/Save button found.")
        self.profile_step_done = True
        self.page.wait_for_timeout(1500)
        self._settle()
        return None

    def _finish_membership(self, how: str) -> EgoJoinResult:
        """We are a member: make the membership usable, then report it."""
        for attempt in range(4):
            readable = self._api_readable()
            if readable["ok"]:
                saved = " (the bot saved the new-member profile step)." if self.profile_step_done else "."
                return self._done("joined", f"{how} -- member, and the community API answers{saved}",
                                  cookies=self._session_cookies())
            if self._on_code_page():
                stopped = self._code_step("before the membership is usable", member=True)
                if stopped:
                    return stopped
                continue
            if not readable["profile_pending"]:
                # Onboarding pages (welcome, pick spaces) can sit between the
                # profile and the community; a Continue-type button moves on.
                if attempt < 3 and self._click_first(PROFILE_CONTINUE_TEXTS):
                    self.page.wait_for_timeout(1500)
                    self._settle()
                    continue
                message = f' "{readable["message"]}"' if readable["message"] else ""
                return self._done("unclear",
                                  f"Member, but the community API answers {readable['status']}{message}.")
            stopped = self._complete_profile_step()
            if stopped:
                return stopped
        return self._done("profile_incomplete", "Profile step still open after 4 passes.")

    # --- the join ------------------------------------------------------------------------

    def _stopped_by_gate(self, state: dict) -> EgoJoinResult | None:
        if state.get("inviteOnlyGate"):
            return self._done("invite_skip", "Private community, no self-serve join -- \"the admin may "
                                             f"need to add you as a member\" ({state.get('url')}).",
                              tag="invite-only-gate")
        if state.get("subscriptionExpired"):
            return self._done("subscription_expired_skip",
                              f"Community's own Circle subscription has lapsed ({state.get('url')}) "
                              "-- nothing to join.", tag="subscription-expired")
        if state.get("challenge"):
            return self._done("challenge_stop", "Cloudflare/human-verification challenge detected -- "
                                                "stopped, no bypass attempted.", tag="challenge")
        if state.get("onCheckout"):
            return self._done("paid_skip", f"Landed on a checkout/paywall URL ({state.get('url')}) "
                                           "-- no free tier reachable.", tag="checkout-no-free-tier")
        return None

    def _open_space_with_join(self) -> bool:
        """Some communities show a signed-in visitor a Join control only inside
        a space. Try up to three that are not events spaces."""
        paths = self._eval(SPACE_LINKS_JS) or []
        origin = self._origin()
        for target in [p for p in paths if "event" not in p.lower()][:3]:
            if self._navigate(origin + target):
                continue
            self._settle()
            if self._page_state().get("hasJoin"):
                return True
        return False

    def _try_signup_form(self, clicked: str) -> EgoJoinResult | dict:
        """Fill a sign-up form that asks for nothing but an email and a
        password. A result when it settled the visit; a dict with the reason
        when the form is anything else."""
        if not (self.email and self.password):
            return {"reason": "no credentials configured"}
        shape = self._eval(SIGNUP_SHAPE_JS, {"emailSelector": EMAIL_SELECTOR}) or {}
        if not shape.get("fillable"):
            return {"reason": shape.get("reason") or "unrecognized form"}
        try:
            if shape.get("hasEmail"):
                self._fill_credential(EMAIL_SELECTOR, self.email)
            self._fill_credential(PASSWORD_SELECTOR, self.password)
        except _ForeignLogin as exc:
            return {"reason": f"the form is on {exc}, not the community's host or circle.so -- "
                              "credentials withheld"}
        if not self._click_first(SIGNUP_SUBMIT_TEXTS):
            return {"reason": "filled the sign-up form but found no submit control"}
        self.page.wait_for_timeout(2500)
        self._settle(12000)
        if self._on_code_page():
            stopped = self._code_step("after the sign-up form", member=True)
            if stopped:
                return stopped
        after = self._post_click_state()
        if after.get("onCheckout"):
            return self._done("paid_skip", f'Clicked "{clicked}", filled the email+password sign-up '
                                           f"form, landed on checkout ({after.get('url')}).")
        ms = self._member_state()
        if ms.get("other_account"):
            return self._done("wrong_account",
                              "After the sign-up form a different Circle account is signed in -- stopped.")
        if ms.get("is_member") or after.get("newMemberOnboarding"):
            return self._finish_membership(f'Clicked "{clicked}" and filled the email+password sign-up form')
        if after.get("pending"):
            return self._done("pending_approval", f'Clicked "{clicked}", filled the sign-up form -- '
                                                  "community shows a pending-approval message.")
        return {"reason": f"submitted the sign-up form but the page ({after.get('url')}) didn't "
                          "match a known pattern"}

    def run(self) -> EgoJoinResult:
        nav = self._navigate(self.url)
        if nav:
            return self._done(nav["status"], nav["detail"])
        self._settle()

        tried_login = False
        tried_space = False
        # Extra rounds: a signed-out visitor sees only Join, which opens a
        # login dialog -- sign in, come back, click Join again; and a signed-in
        # non-member whose page has no Join control -- step into a space.
        for round_no in range(3):
            if self._on_code_page():
                stopped = self._code_step("on the community page", member=False)
                if stopped:
                    return stopped
            state = self._page_state()
            gate = self._stopped_by_gate(state)
            if gate:
                return gate
            ms = self._member_state()
            if ms.get("other_account"):
                return self._done("wrong_account", f"Another Circle account is signed in on "
                                                   f"{state.get('url')} -- not joining as it.")
            if ms.get("is_member"):
                return self._finish_membership("Signed in; already a member" if round_no
                                               else "Already a member")
            signed_out = (not ms.get("logged_in")) if ms["api"] else state.get("hasLogin")
            if signed_out and (state.get("hasLogin") or ms["api"]) and not tried_login:
                tried_login = True
                stopped = self._sign_in(has_login_control=bool(state.get("hasLogin")))
                if stopped:
                    return stopped
                continue
            if not state.get("hasJoin") and ms["api"] and ms.get("logged_in") and not tried_space:
                tried_space = True
                if self._open_space_with_join():
                    continue
            if not state.get("hasJoin"):
                who = (", not a Circle community page" if not ms["api"]
                       else ", signed in, not a member" if ms.get("logged_in") else ", signed out")
                return self._done("unclear", f"No Join control found ({state.get('url')}{who}) "
                                             "-- needs a human look.")
            clicked = self._click_first(JOIN_TEXTS)
            if not clicked:
                return self._done("unclear", "A Join-like control was detected but could not be clicked.",
                                  tag="join-control-not-clickable")
            self.page.wait_for_timeout(1500)
            self._settle()
            # Circle mails a code on the join step too (erkshtest, 2026-09-25).
            if self._on_code_page():
                stopped = self._code_step("after the Join click", member=True)
                if stopped:
                    return stopped
            after = self._post_click_state()
            if after.get("onCheckout"):
                return self._done("paid_skip", f'Clicked "{clicked}" but landed on checkout '
                                               f"({after.get('url')}) -- no free tier reachable.",
                                  tag="after-join")
            ms2 = self._member_state()
            if ms2.get("other_account"):
                return self._done("wrong_account",
                                  "After the Join click a different Circle account is signed in -- stopped.")
            if ms2.get("is_member") or after.get("newMemberOnboarding"):
                return self._finish_membership(f'Clicked "{clicked}"')
            if after.get("pending"):
                return self._done("pending_approval",
                                  f'Clicked "{clicked}" -- community shows a pending-approval message.',
                                  tag="after-join")
            if ms2["api"] and not ms2.get("logged_in") and not tried_login:
                tried_login = True
                stopped = self._sign_in()
                if stopped:
                    return stopped
                back = self._navigate(self.url)
                if back:
                    return self._done("nav_failed" if back["status"] == "dead_host" else back["status"],
                                      back["detail"])
                self._settle()
                continue
            if after.get("formQuestions"):
                signup = self._try_signup_form(clicked)
                if isinstance(signup, EgoJoinResult):
                    return signup
                return self._done("application_form_detected",
                                  f'Clicked "{clicked}"; a form appeared and was not filled '
                                  f"({signup['reason']}). Questions: "
                                  + json.dumps(after["formQuestions"][:10], ensure_ascii=False))
            return self._done("unclear", f'Clicked "{clicked}" but Circle does not report a membership '
                                         f"({after.get('url')}) -- needs a human look.", tag="after-join")
        return self._done("unclear", "Signed in, but the Join step did not produce a membership "
                                     "-- needs a human look.")


def join_community(page, url: str, *, email: str | None, password: str | None,
                   fetch_code: Callable[[datetime], str | None] | None = None,
                   answer_profile: Callable[[list[FormField]], Any] | None = None,
                   screenshot_dir: str | None = None,
                   now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> EgoJoinResult:
    """Walk one community to a membership, or to the first thing that stops it.

    ``fetch_code(since)`` returns the code Circle mailed after ``since`` (or
    None); ``answer_profile(fields)`` returns a ``forms.FormResolution``. A
    page-level surprise comes back as ``driver_error``, never as an exception:
    it is about this community, not about the batch.
    """
    walk = _Walk(page, url, email=email, password=password, fetch_code=fetch_code,
                 answer_profile=answer_profile, screenshot_dir=screenshot_dir, now=now)
    try:
        return walk.run()
    except Exception as exc:  # noqa: BLE001 - see the docstring
        logger.warning("join walk on %s failed: %s", url, exc, exc_info=True)
        return EgoJoinResult(status="driver_error",
                             detail=f"Driver error: {str(exc)[:300]}", screenshot=walk._shot("driver-error"))
