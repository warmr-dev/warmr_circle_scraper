"""Tests for the browser-session feed reader.

These test the normalization and record shaping without launching a real
browser; the Playwright interaction is exercised via a fake reader.
"""

import pytest

from circle_leads.scraper.browser_reader import (
    BrowserFeedReader,
    _extract_text,
    fetch_space_posts,
)

FEED = [
    {
        "id": 25182862, "name": "CTO positions in France",
        "truncated_content": "We are currently hiring for a number of CTO and Head of Software Engineering positions",
        "created_at": "2026-08-30T10:00:00Z",
        "user": {"id": 7, "name": "Recruiter"},
        "url": "/c/job-posts/cto",
    },
    {
        "id": 32943755, "name": "Startups",
        "truncated_content": "Could you assist me finding engineers, website developers for startups?",
        "created_at": "2026-08-29T10:00:00Z",
    },
]


class FakeReader:
    """Stand-in for BrowserFeedReader that returns canned records."""

    base = "https://startupandangels.circle.so"

    def __init__(self, records):
        self._records = records

    def list_posts(self, space_id, *, max_pages=10):
        return self._records


def test_extract_text_reads_name_and_truncated_content():
    title, body = _extract_text(FEED[0])
    assert title == "CTO positions in France"
    assert "hiring" in body


def test_fetch_space_posts_normalizes():
    records = fetch_space_posts(FakeReader(FEED), 1595123)
    assert len(records) == 2
    first = records[0]
    assert first["source_content_id"] == "25182862"
    assert first["url"] == "https://startupandangels.circle.so/c/job-posts/cto"
    assert first["author"]["display_name"] == "Recruiter"
    assert first["permission_reference"] == "browser_session"


def test_fetch_skips_empty_posts():
    records = fetch_space_posts(FakeReader([{"id": 1, "name": "", "truncated_content": ""}]), 1)
    assert records == []


def test_reader_uses_persistent_profile(tmp_path):
    """The reader must point at a persistent profile dir, not a fresh one."""
    reader = BrowserFeedReader("x.circle.so", profile_dir=tmp_path / "prof")
    assert reader.profile_dir.exists()
    assert reader.base == "https://x.circle.so"


def test_reader_never_takes_a_cookie_argument():
    """By design the reader has no cookie/token parameter -- the browser holds it."""
    import inspect

    sig = inspect.signature(BrowserFeedReader.__init__)
    assert "cookie" not in sig.parameters
    assert "token" not in sig.parameters
    assert "session" not in sig.parameters
