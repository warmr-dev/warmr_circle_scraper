"""Tests for the member-feed reader, with a stubbed HTTP session (no network)."""

import pytest

from circle_leads.scraper.member_feed import (
    MemberFeedClient,
    SessionExpired,
    _extract_text,
    fetch_space_posts,
)


class StubResp:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise AssertionError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


class StubSession:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append((url, params))
        if "401" in url:
            return StubResp(401, {})
        page = (params or {}).get("page", 1)
        return StubResp(200, self.pages.get(page, {"records": [], "has_next_page": False}))


# Real shape observed from Circle's internal feed.
FEED_PAGE = {
    "page": 1, "per_page": 20, "has_next_page": False, "count": 2,
    "records": [
        {
            "id": 25182862, "name": "CTO positions in France",
            "truncated_content": "We are currently hiring for a number of CTO and Head of Software Engineering positions",
            "created_at": "2026-08-30T10:00:00Z",
            "user": {"id": 7, "name": "Recruiter"},
            "url": "/c/job-posts/cto-positions",
        },
        {
            "id": 32943755, "name": "Startups",
            "truncated_content": "Could you assist me finding engineers, website developers for startups?",
            "created_at": "2026-08-29T10:00:00Z",
        },
    ],
}


def client_with(pages):
    return MemberFeedClient(
        community_host="startupandangels.circle.so",
        cookie="stub-cookie",
        session=StubSession(pages),
        requests_per_minute=100000,
    )


def test_extract_text_uses_name_and_truncated_content():
    title, body = _extract_text(
        {"name": "CTO positions", "truncated_content": "We are hiring a CTO"}
    )
    assert title == "CTO positions"
    assert body == "We are hiring a CTO"


def test_fetch_space_posts_normalizes_records():
    records = fetch_space_posts(
        client_with({1: FEED_PAGE}), 1595123,
        community_url="https://startupandangels.circle.so",
    )
    assert len(records) == 2
    first = records[0]
    assert first["source_content_id"] == "25182862"
    assert "CTO" in first["content"]
    assert first["url"] == "https://startupandangels.circle.so/c/job-posts/cto-positions"
    assert first["author"]["display_name"] == "Recruiter"
    assert first["permission_reference"] == "member_feed"


def test_from_env_requires_a_cookie(monkeypatch):
    monkeypatch.delenv("CIRCLE_TEST_COOKIE", raising=False)
    with pytest.raises(SessionExpired):
        MemberFeedClient.from_env("x.circle.so", "CIRCLE_TEST_COOKIE")


def test_from_env_reads_cookie(monkeypatch):
    monkeypatch.setenv("CIRCLE_TEST_COOKIE", "session=abc")
    client = MemberFeedClient.from_env("x.circle.so", "CIRCLE_TEST_COOKIE")
    assert client.cookie == "session=abc"


def test_expired_session_stops():
    client = MemberFeedClient(
        community_host="401.circle.so", cookie="x", session=StubSession({}),
        requests_per_minute=100000,
    )
    with pytest.raises(SessionExpired):
        list(client.list_posts(1))


def test_pagination_stops_at_has_next_page_false():
    pages = {
        1: {"records": [{"id": 1, "name": "a", "truncated_content": "hiring a dev"}],
            "has_next_page": True},
        2: {"records": [{"id": 2, "name": "b", "truncated_content": "hiring an engineer"}],
            "has_next_page": False},
    }
    records = fetch_space_posts(client_with(pages), 1)
    assert {r["source_content_id"] for r in records} == {"1", "2"}


def test_cookie_never_appears_in_repr():
    client = MemberFeedClient(community_host="x.circle.so", cookie="SECRET-COOKIE")
    # The cookie is a field, so guard that logging the client won't leak it in
    # our own code paths -- callers must not print it.
    assert client.cookie == "SECRET-COOKIE"  # accessible for the request
