"""Alerts: quiet by default, never fatal, and never a flood.

The point of these is the opposite of most tests. What matters is not that a
message arrives, but that nothing bad happens when it cannot.
"""
from __future__ import annotations

import pytest
import requests

from circle_leads.notify import telegram


@pytest.fixture(autouse=True)
def clean_state(monkeypatch):
    telegram._sent_at.clear()
    telegram._last_seen.clear()
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    yield
    telegram._sent_at.clear()
    telegram._last_seen.clear()


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")


class Posted:
    """Records calls instead of reaching Telegram."""

    def __init__(self, status=200):
        self.calls = []
        self.status = status

    def __call__(self, url, json=None, timeout=None):
        self.calls.append({"url": url, "json": json})

        class R:
            status_code = self.status
            text = "{}"
        return R()


def test_no_token_means_no_call_and_no_error(monkeypatch):
    def explode(*a, **k):
        raise AssertionError("must not reach the network")

    monkeypatch.setattr(requests, "post", explode)
    assert telegram.send("anything") is False


def test_a_token_without_a_chat_id_is_still_off(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    posted = Posted()
    monkeypatch.setattr(requests, "post", posted)
    assert telegram.send("anything") is False
    assert posted.calls == []


def test_a_configured_bot_sends_once(monkeypatch, configured):
    posted = Posted()
    monkeypatch.setattr(requests, "post", posted)
    assert telegram.send("hello") is True
    assert len(posted.calls) == 1
    assert posted.calls[0]["json"]["chat_id"] == "42"
    assert posted.calls[0]["json"]["text"] == "hello"


def test_a_network_failure_is_not_raised(monkeypatch, configured):
    """A messaging outage must not look like a pipeline failure."""
    def boom(*a, **k):
        raise requests.ConnectionError("telegram is down")

    monkeypatch.setattr(requests, "post", boom)
    assert telegram.send("hello") is False       # no exception


def test_an_http_error_is_not_raised(monkeypatch, configured):
    posted = Posted(status=403)
    monkeypatch.setattr(requests, "post", posted)
    assert telegram.send("hello") is False


def test_the_same_message_is_not_repeated(monkeypatch, configured):
    """A community failing every two minutes would send 720 a day otherwise."""
    posted = Posted()
    monkeypatch.setattr(requests, "post", posted)
    for _ in range(5):
        telegram.send("host X is refusing us")
    assert len(posted.calls) == 1


def test_a_different_message_still_gets_through(monkeypatch, configured):
    posted = Posted()
    monkeypatch.setattr(requests, "post", posted)
    telegram.send("first")
    telegram.send("second")
    assert len(posted.calls) == 2


def test_dedup_can_be_keyed_separately_from_the_text(monkeypatch, configured):
    posted = Posted()
    monkeypatch.setattr(requests, "post", posted)
    telegram.send("lead: 12:04", dedup_key="lead:url-1")
    telegram.send("lead: 12:06", dedup_key="lead:url-1")   # same lead, new text
    telegram.send("lead: 12:07", dedup_key="lead:url-2")
    assert len(posted.calls) == 2


def test_the_per_minute_cap_holds(monkeypatch, configured):
    posted = Posted()
    monkeypatch.setattr(requests, "post", posted)
    for i in range(40):
        telegram.send(f"message {i}")
    assert len(posted.calls) == telegram.MAX_PER_MINUTE


def test_a_long_message_is_cut_to_the_limit(monkeypatch, configured):
    posted = Posted()
    monkeypatch.setattr(requests, "post", posted)
    telegram.send("x" * 9000)
    assert len(posted.calls[0]["json"]["text"]) == telegram.MAX_LEN


def test_html_special_characters_are_escaped():
    assert telegram.escape('<b>a & "b"</b>') == "&lt;b&gt;a &amp; &quot;b&quot;&lt;/b&gt;"


def test_notify_puts_the_title_in_bold(monkeypatch, configured):
    posted = Posted()
    monkeypatch.setattr(requests, "post", posted)
    telegram.notify("Something broke", "details here", level="error")
    text = posted.calls[0]["json"]["text"]
    assert "<b>Something broke</b>" in text
    assert "details here" in text


def test_an_empty_message_is_not_sent(monkeypatch, configured):
    posted = Posted()
    monkeypatch.setattr(requests, "post", posted)
    assert telegram.send("   ") is False
    assert posted.calls == []
