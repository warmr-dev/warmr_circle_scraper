"""Cookie parsing and the encrypted session store (circle_leads/web/replay_store.py).

No network and no real browser here. These lock down the security-relevant
behaviour: sessions are stored only encrypted (or plainly, by explicit choice),
and cookie metadata (never the cookies) is what list_sessions() exposes. This
module is live, load-bearing code -- scanning.py and the auto-join bot use it
directly -- unlike the now-deleted /api/replay/* dashboard routes that used to
be tested here too; see the note further down.
"""

from __future__ import annotations

import json
import tempfile

import pytest

from circle_leads.remote_browser.replay import (
    has_session_cookie, parse_cookies, _primary_host,
)


SAMPLE = [
    {"domain": "www.yourspinstate.com", "name": "_circle_session", "path": "/",
     "secure": True, "httpOnly": True, "sameSite": "no_restriction",
     "session": True, "value": "abc"},
    {"domain": "www.yourspinstate.com", "name": "remember_user_token", "path": "/",
     "secure": True, "httpOnly": True, "sameSite": "lax", "session": False,
     "expirationDate": 1820322582.85, "value": "def"},
    {"domain": ".yourspinstate.com", "name": "_ga", "path": "/",
     "secure": False, "sameSite": "unspecified", "session": False,
     "expirationDate": 1820322582.0, "value": "GA1.1"},
]


# --- parsing ---------------------------------------------------------------

def test_parse_maps_samesite_and_expiry():
    cookies = parse_cookies(json.dumps(SAMPLE))
    by_name = {c["name"]: c for c in cookies}
    assert by_name["_circle_session"]["sameSite"] == "None"
    assert by_name["remember_user_token"]["sameSite"] == "Lax"
    # session cookie has no expires; persistent one does
    assert "expires" not in by_name["_circle_session"]
    assert by_name["remember_user_token"]["expires"] == pytest.approx(1820322582.85)
    # 'unspecified' sameSite is dropped rather than sent as a bad value
    assert "sameSite" not in by_name["_ga"]


def test_parse_rejects_non_arrays():
    with pytest.raises(ValueError):
        parse_cookies('{"not":"an array"}')
    with pytest.raises(ValueError):
        parse_cookies("[]")


def test_has_session_cookie_detects_circle_session():
    assert has_session_cookie(parse_cookies(json.dumps(SAMPLE))) is True
    analytics_only = [c for c in SAMPLE if c["name"] == "_ga"]
    assert has_session_cookie(parse_cookies(json.dumps(analytics_only))) is False


def test_primary_host_from_session_cookie():
    assert _primary_host(parse_cookies(json.dumps(SAMPLE))) == "www.yourspinstate.com"


# --- encrypted store -------------------------------------------------------

def _db():
    from circle_leads.storage.database import Database
    return Database("sqlite:///" + tempfile.mktemp(suffix=".db"))


def test_store_without_a_key_stores_plaintext(monkeypatch):
    """No key -> plaintext blob (marked), round-trips fine."""
    monkeypatch.delenv("CIRCLE_CRED_KEY", raising=False)
    from circle_leads.web.replay_store import store_session, load_cookies
    db = _db()
    cookies = parse_cookies(json.dumps(SAMPLE))
    store_session(db, "x.circle.so", cookies)
    assert load_cookies(db, "x.circle.so") == cookies


@pytest.mark.skipif(
    __import__("importlib").util.find_spec("cryptography") is None,
    reason="cryptography not installed",
)
def test_store_encrypts_and_round_trips(monkeypatch):
    monkeypatch.setenv("CIRCLE_CRED_KEY", "a-strong-key")
    from circle_leads.web.replay_store import (
        load_cookies, store_session, list_sessions,
    )
    db = _db()
    cookies = parse_cookies(json.dumps(SAMPLE))
    store_session(db, "x.circle.so", cookies, member_label="Me")

    # The raw DB value must be ciphertext, not the cookie plaintext.
    from sqlalchemy import select
    from circle_leads.storage.models import ReplaySession
    with db.session() as s:
        row = s.scalar(select(ReplaySession).where(ReplaySession.host == "x.circle.so"))
        assert "_circle_session" not in row.encrypted_cookies
        assert "abc" not in row.encrypted_cookies

    assert load_cookies(db, "x.circle.so") == cookies
    meta = list_sessions(db)[0]
    assert meta["cookie_count"] == 3 and meta["member_label"] == "Me"
    assert "encrypted_cookies" not in meta  # never exposed


@pytest.mark.skipif(
    __import__("importlib").util.find_spec("cryptography") is None,
    reason="cryptography not installed",
)
def test_plaintext_sessions_are_encrypted_in_place(monkeypatch):
    """44 sessions sat in plaintext until the key was set (2026-09-30)."""
    from sqlalchemy import select

    from circle_leads.storage.models import ReplaySession
    from circle_leads.web.replay_store import (
        ReplayKeyMissing, encrypt_plaintext_sessions, load_cookies, store_session,
    )

    db = _db()
    cookies = parse_cookies(json.dumps(SAMPLE))
    monkeypatch.delenv("CIRCLE_CRED_KEY", raising=False)
    store_session(db, "a.circle.so", cookies, member_label="4")
    store_session(db, "b.circle.so", cookies)
    with pytest.raises(ReplayKeyMissing):
        encrypt_plaintext_sessions(db)
    with db.session() as s:
        before = {r.host: r.updated_at for r in s.scalars(select(ReplaySession))}

    monkeypatch.setenv("CIRCLE_CRED_KEY", "a-strong-key")
    assert encrypt_plaintext_sessions(db) == 2
    assert encrypt_plaintext_sessions(db) == 0
    with db.session() as s:
        for row in s.scalars(select(ReplaySession)):
            assert not row.encrypted_cookies.startswith("plain:")
            assert "_circle_session" not in row.encrypted_cookies
            assert row.updated_at == before[row.host]
    assert load_cookies(db, "a.circle.so") == cookies
    assert load_cookies(db, "b.circle.so") == cookies


def _label(db, host):
    from circle_leads.web.replay_store import list_sessions
    return next(m["member_label"] for m in list_sessions(db) if m["host"] == host)


def test_a_refresh_that_names_no_account_keeps_the_one_on_record(monkeypatch):
    """The extension sends cookies without saying whose they are, and every
    refresh used to erase the account the bot had written."""
    monkeypatch.delenv("CIRCLE_CRED_KEY", raising=False)
    from circle_leads.web.replay_store import store_session
    db = _db()
    cookies = parse_cookies(json.dumps(SAMPLE))
    store_session(db, "x.circle.so", cookies, member_label="test")
    store_session(db, "x.circle.so", cookies)
    assert _label(db, "x.circle.so") == "test"
    store_session(db, "x.circle.so", cookies, member_label="main")
    assert _label(db, "x.circle.so") == "main"


def test_a_new_session_takes_the_account_the_community_was_joined_with(monkeypatch):
    monkeypatch.delenv("CIRCLE_CRED_KEY", raising=False)
    from circle_leads.storage.database import get_or_create_community
    from circle_leads.web.replay_store import store_session
    db = _db()
    with db.session() as s:
        c = get_or_create_community(s, slug="x", url="https://x.circle.so")
        c.host, c.join_account = "x.circle.so", "test"
    store_session(db, "x.circle.so", parse_cookies(json.dumps(SAMPLE)))
    assert _label(db, "x.circle.so") == "test"
    store_session(db, "y.circle.so", parse_cookies(json.dumps(SAMPLE)))
    assert _label(db, "y.circle.so") is None


# The /api/replay/* routes that used to be tested here (built by the now-
# deleted circle_leads/web/remote_browser_api.py) were removed outright: the
# "Session replay experiment (Version B)" panel was explicitly labeled an
# experiment and was the only caller of those routes. The encrypted store
# above (replay_store.py) is untouched -- it's the live mechanism scanning.py
# and the auto-join bot use directly, with no HTTP route needed.
