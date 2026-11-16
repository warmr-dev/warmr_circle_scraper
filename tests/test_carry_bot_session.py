"""What carry_bot_session.py takes from the bot's browser, and what it leaves."""
import importlib.util
import io
import json
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[1] / "deploy" / "droplet" / "carry_bot_session.py"
_spec = importlib.util.spec_from_file_location("carry_bot_session", _PATH)
carry = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(carry)


def _c(name, domain, path="/"):
    return {"name": name, "value": "v", "domain": domain, "path": path, "expires": -1,
            "httpOnly": True, "secure": True, "sameSite": "Lax"}


PROFILE = [
    # the central login and two communities, one on its own domain
    _c("_circle_session", "login.circle.so"),
    _c("remember_user_token", "login.circle.so"),
    _c("circle_last_login_method_1", ".circle.so"),
    _c("_circle_session", "community.example.com"),
    _c("remember_user_token", "community.example.com"),
    _c("cookie_consent_displayed", ".example.com"),   # the community's own banner
    _c("_circle_session", "space.circle.so"),
    # Cloudflare's checks, on Circle hosts
    _c("cf_clearance", ".circle.so"),
    _c("__cf_bm", "community.example.com"),
    _c("cf_chl_rc_m", "login.circle.so"),
    # not Circle at all
    _c("SID", ".google.com"),
    _c("session-token", ".amazon.com"),
    _c("ahoy_visitor", "unrelated.circle-lookalike.com"),
]


def _names(cookies):
    return sorted((c["domain"], c["name"]) for c in cookies)


def test_only_circle_cookies_are_carried_and_never_cloudflares():
    assert _names(carry.circle_cookies(PROFILE)) == sorted([
        ("login.circle.so", "_circle_session"),
        ("login.circle.so", "remember_user_token"),
        (".circle.so", "circle_last_login_method_1"),
        ("community.example.com", "_circle_session"),
        ("community.example.com", "remember_user_token"),
        (".example.com", "cookie_consent_displayed"),
        ("space.circle.so", "_circle_session"),
    ])


def test_a_parent_domain_alone_does_not_make_a_site_circles():
    # .example.com is carried only because community.example.com holds a session
    no_session = [c for c in PROFILE if c["domain"] != "community.example.com"]
    assert (".example.com", "cookie_consent_displayed") not in _names(carry.circle_cookies(no_session))


def test_export_refuses_a_terminal(monkeypatch, capsys):
    class Tty(io.StringIO):
        def isatty(self):
            return True

    monkeypatch.setattr(carry.sys, "stdout", Tty())
    assert carry.main(["export"]) == 2
    assert "never into a terminal" in capsys.readouterr().err


def test_summary_names_the_central_session():
    assert "central login session: yes" in carry.summary(carry.circle_cookies(PROFILE))
    no_central = [c for c in PROFILE if c["domain"] != "login.circle.so"]
    assert "central login session: NO" in carry.summary(carry.circle_cookies(no_central))


def test_import_filters_again_whatever_it_is_given(monkeypatch, capsys):
    added = []

    class Context:
        def add_cookies(self, cookies):
            added.extend(cookies)

        def cookies(self):
            return added

    class Chromium:
        def connect_over_cdp(self, cdp):
            return type("B", (), {"contexts": [Context()]})()

    class Playwright:
        chromium = Chromium()

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    sync_api = pytest.importorskip("playwright.sync_api")
    monkeypatch.setattr(sync_api, "sync_playwright", lambda: Playwright())
    monkeypatch.setattr(carry.sys, "stdin", io.StringIO(json.dumps(PROFILE)))
    assert carry.main(["import"]) == 0
    assert ("_circle_session", "login.circle.so") in {(c["name"], c["domain"]) for c in added}
    assert not any(carry.is_cloudflare(c) or "google" in c["domain"] for c in added)
    assert "not taken by the browser: 0" in capsys.readouterr().out
