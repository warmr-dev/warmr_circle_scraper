"""The mailbox side of a login: pick Circle's 6-digit code out of an inbox
without touching anything else in it. No real IMAP server -- ``FakeIMAP``
records every call, so the tests can also assert what the module does *not* do
(mark mail read, delete, send).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from email.message import EmailMessage

import pytest

from circle_leads.join.email_code import (
    EmailCodeError,
    extract_code,
    fetch_login_code,
    message_sent_at,
)

NOW = datetime(2026, 9, 25, 10, 0, tzinfo=timezone.utc)


def _message(*, subject: str, body: str, sent_at: datetime, to: str, html: bool = False) -> bytes:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = "no-reply@circle.so"
    msg["To"] = to
    msg["Date"] = sent_at.strftime("%a, %d %b %Y %H:%M:%S %z")
    if html:
        msg.set_content("plain text fallback")
        msg.add_alternative(body, subtype="html")
    else:
        msg.set_content(body)
    return msg.as_bytes()


class FakeIMAP:
    """Minimal IMAP4_SSL stand-in: it serves ``messages`` and logs the calls."""

    def __init__(self, messages: list[bytes]):
        self.messages = messages
        self.calls: list[tuple] = []
        self.logged_out = False

    def __call__(self, host):  # used as imap_factory
        self.calls.append(("connect", host))
        return self

    def login(self, user, password):
        self.calls.append(("login", user))
        return "OK", [b"logged in"]

    def select(self, mailbox, readonly=False):
        self.calls.append(("select", mailbox, readonly))
        return "OK", [b"1"]

    def search(self, charset, *criteria):
        self.calls.append(("search", criteria))
        ids = b" ".join(str(i + 1).encode() for i in range(len(self.messages)))
        return "OK", [ids]

    def fetch(self, uid, spec):
        self.calls.append(("fetch", uid, spec))
        index = int(uid) - 1
        return "OK", [(b"1 (BODY[] {123}", self.messages[index]), b")"]

    def logout(self):
        self.logged_out = True
        return "BYE", [b"logged out"]


@pytest.fixture
def creds(monkeypatch):
    monkeypatch.setenv("CIRCLE_MAIL_USER", "erkshtest@gmail.com")
    monkeypatch.setenv("CIRCLE_MAIL_APP_PASSWORD", "app-password")


def test_code_next_to_the_word_code_wins_over_other_six_digit_numbers():
    msg = EmailMessage()
    msg["Subject"] = "Your Circle account"
    msg.set_content("Order 998877 shipped.\nYour verification code is 314159.\n")
    assert extract_code(msg) == "314159"


def test_code_is_read_out_of_an_html_only_body():
    raw = _message(
        subject="Confirm your login",
        body="<html><body><p>Your code: <b>246810</b></p></body></html>",
        sent_at=NOW,
        to="erkshtest@gmail.com",
        html=True,
    )
    import email as email_module

    assert extract_code(email_module.message_from_bytes(raw)) == "246810"


def test_message_without_a_code_yields_none():
    msg = EmailMessage()
    msg["Subject"] = "Welcome to the community"
    msg.set_content("Say hello in the intro space.")
    assert extract_code(msg) is None


def test_fetch_returns_the_code_from_a_message_sent_after_the_login_started(creds):
    fake = FakeIMAP(
        [
            _message(
                subject="Your Circle verification code",
                body="Code: 123456",
                sent_at=NOW + timedelta(seconds=30),
                to="erkshtest@gmail.com",
            )
        ]
    )
    code = fetch_login_code(
        "erkshtest@gmail.com",
        since=NOW,
        imap_factory=fake,
        now=lambda: NOW,
        sleep=lambda _s: None,
    )
    assert code == "123456"
    assert fake.logged_out


def test_a_code_from_an_earlier_run_is_ignored(creds):
    """A stale code typed into a fresh challenge fails the login and burns an
    attempt -- so anything older than ``since`` must not come back."""
    fake = FakeIMAP(
        [
            _message(
                subject="Your Circle verification code",
                body="Code: 111111",
                sent_at=NOW - timedelta(hours=2),
                to="erkshtest@gmail.com",
            )
        ]
    )
    calls = {"n": 0}

    def clock():
        calls["n"] += 1
        return NOW if calls["n"] < 3 else NOW + timedelta(seconds=300)

    assert (
        fetch_login_code(
            "erkshtest@gmail.com",
            since=NOW,
            timeout_seconds=1,
            imap_factory=fake,
            now=clock,
            sleep=lambda _s: None,
        )
        is None
    )


def test_timeout_returns_none_instead_of_raising(creds):
    fake = FakeIMAP([])
    times = iter([NOW, NOW + timedelta(seconds=999)])
    assert (
        fetch_login_code(
            "erkshtest@gmail.com",
            since=NOW,
            timeout_seconds=1,
            imap_factory=fake,
            now=lambda: next(times),
            sleep=lambda _s: None,
        )
        is None
    )


def test_mailbox_is_opened_read_only_and_never_marked_read(creds):
    """BODY.PEEK and readonly=True are the whole promise of this module: the
    operator's own mail must look untouched afterwards."""
    fake = FakeIMAP(
        [
            _message(
                subject="Your Circle verification code",
                body="Code: 654321",
                sent_at=NOW,
                to="erkshtest@gmail.com",
            )
        ]
    )
    fetch_login_code(
        "erkshtest@gmail.com",
        since=NOW - timedelta(minutes=1),
        imap_factory=fake,
        now=lambda: NOW,
        sleep=lambda _s: None,
    )
    select = next(c for c in fake.calls if c[0] == "select")
    assert select[2] is True
    fetches = [c for c in fake.calls if c[0] == "fetch"]
    assert fetches and all(c[2] == "(BODY.PEEK[])" for c in fetches)
    assert not any(c[0] in {"store", "copy", "expunge"} for c in fake.calls)


def test_search_is_scoped_to_the_bot_address(creds):
    fake = FakeIMAP([])
    fetch_login_code(
        "erkshtest+7@gmail.com",
        since=NOW,
        timeout_seconds=0,
        imap_factory=fake,
        now=lambda: NOW + timedelta(seconds=1),
        sleep=lambda _s: None,
    )
    search = next(c for c in fake.calls if c[0] == "search")
    assert '"erkshtest+7@gmail.com"' in search[1]


def test_missing_credentials_say_what_to_set(monkeypatch):
    monkeypatch.delenv("CIRCLE_MAIL_USER", raising=False)
    monkeypatch.delenv("CIRCLE_MAIL_APP_PASSWORD", raising=False)
    with pytest.raises(EmailCodeError, match="CIRCLE_MAIL_APP_PASSWORD"):
        fetch_login_code("erkshtest@gmail.com", since=NOW)


def test_message_sent_at_handles_a_date_without_a_timezone():
    msg = EmailMessage()
    msg["Date"] = "Thu, 25 Sep 2026 10:00:00"
    assert message_sent_at(msg) == NOW
