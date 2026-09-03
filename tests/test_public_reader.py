"""Tests for the public (no-login) community reader, with a stubbed session."""

import pytest

from circle_leads.scraper.public_reader import (
    PublicReader,
    discover_and_read_public,
    normalize_public_post,
)


class StubResp:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class StubSession:
    def __init__(self, routes):
        self.routes = routes  # {url-substring: StubResp}

    def get(self, url, headers=None, timeout=None):
        path = url.split("?")[0]
        # Exact-endpoint match: the spaces-list route matches only the bare
        # /internal_api/spaces path, so /spaces/<id>/posts can't collide with it.
        for key, resp in self.routes.items():
            if key.endswith("/spaces") and path.endswith(key):
                return resp
        for key, resp in self.routes.items():
            if not key.endswith("/spaces") and key in url:
                return resp
        return StubResp(404, None)


SPACES = {
    "records": [
        {"id": 1317145, "slug": "i-need-a-consultant", "name": "I Need A Consultant"},
        {"id": 940983, "slug": "client-acquisition", "name": "Client Acquisition Chat"},
        {"id": 972593, "slug": "chat", "name": "General Chat"},
    ]
}
CONSULTANT_POSTS = {
    "records": [
        {"id": 1, "name": "Senior Data Engineer needed",
         "truncated_content": "We are hiring a senior data engineer for a finance project, Databricks",
         "created_at": "2026-08-30T10:00:00Z", "user": {"id": 5, "name": "Client"}},
        {"id": 2, "name": "AI engineer for healthcare",
         "truncated_content": "Looking for an AI engineer to build our healthcare platform",
         "created_at": "2026-08-29T10:00:00Z"},
    ],
    "has_next_page": False,
}


def reader_with(routes):
    return PublicReader(
        community_host="x.circle.so",
        session=StubSession(routes),
        requests_per_minute=100000,
    )


def test_list_spaces_parses_public_list():
    r = reader_with({"/internal_api/spaces": StubResp(200, SPACES)})
    spaces = r.list_spaces()
    assert len(spaces) == 3
    assert any(s.name == "I Need A Consultant" for s in spaces)


def test_read_space_returns_public_posts():
    r = reader_with({
        "/spaces/1317145/posts": StubResp(200, CONSULTANT_POSTS),
    })
    was_public, recs = r.read_space(1317145)
    assert was_public is True
    assert len(recs) == 2


def test_private_space_is_detected_and_skipped():
    r = reader_with({"/spaces/940983/posts": StubResp(401, None)})
    was_public, recs = r.read_space(940983)
    assert was_public is False
    assert recs == []


def test_discover_reads_only_lead_spaces_and_skips_private():
    routes = {
        "/internal_api/spaces": StubResp(200, SPACES),
        "/spaces/1317145/posts": StubResp(200, CONSULTANT_POSTS),  # public lead space
        "/spaces/940983/posts": StubResp(401, None),                # private lead space
    }
    spaces, records = discover_and_read_public(
        reader_with(routes), only_lead_spaces=True
    )
    # Both consultant and client-acquisition match lead hints; general chat does not.
    consultant = next(s for s in spaces if s.id == "1317145")
    private = next(s for s in spaces if s.id == "940983")
    general = next(s for s in spaces if s.id == "972593")
    assert consultant.is_public is True
    assert private.is_public is False          # 401 -> skipped
    assert general.is_public is None           # never a target, never probed
    assert len(records) == 2                    # only the public consultant posts
    assert any("data engineer" in r["content"].lower() for r in records)


def test_normalize_public_post_shapes_record():
    rec = normalize_public_post(
        CONSULTANT_POSTS["records"][0], community_url="https://x.circle.so"
    )
    assert rec["source_content_id"] == "1"
    assert rec["permission_reference"] == "public_space"
    assert "data engineer" in rec["content"].lower()


def test_no_public_space_list_returns_empty():
    r = reader_with({"/internal_api/spaces": StubResp(403, None)})
    assert r.list_spaces() == []
    spaces, records = discover_and_read_public(r)
    assert spaces == [] and records == []


# --- Comment reading and tiptap extraction ---------------------------------

def test_tiptap_body_is_flattened():
    """Comments store text in tiptap_body (nested under 'body'), not truncated_content."""
    from circle_leads.scraper.public_reader import _extract_text
    record = {
        "id": 1,
        "tiptap_body": {"body": {"type": "doc", "content": [
            {"type": "paragraph", "content": [
                {"type": "text", "text": "We need a backend engineer for this"},
            ]},
        ]}},
    }
    _, body = _extract_text(record)
    assert "backend engineer" in body


def test_read_comments_uses_correct_endpoint():
    from circle_leads.scraper.public_reader import PublicReader

    called = {}
    class Sess:
        def get(self, url, **kw):
            called["url"] = url
            class R:
                status_code = 200
                def json(self): return {"records": [{"id": 9}]}
            return R()

    r = PublicReader("x.circle.so", session=Sess(), requests_per_minute=100000)
    comments = r.read_comments(42)
    assert "/internal_api/posts/42/comments" in called["url"]
    assert len(comments) == 1


# --- Recency / incremental cutoff -------------------------------------------

def test_read_space_stops_at_recency_cutoff():
    from datetime import datetime, timedelta
    from circle_leads.scraper.public_reader import PublicReader

    now = datetime(2026, 9, 1)
    page = {
        "records": [
            {"id": 1, "name": "new", "truncated_content": "recent",
             "created_at": "2026-08-30T10:00:00Z"},   # inside window
            {"id": 2, "name": "old", "truncated_content": "stale",
             "created_at": "2026-01-01T10:00:00Z"},   # outside window -> stop
            {"id": 3, "name": "older", "truncated_content": "x",
             "created_at": "2025-06-01T10:00:00Z"},
        ],
        "has_next_page": True,
    }

    class Sess:
        def get(self, url, **kw):
            class R:
                status_code = 200
                def json(self): return page
            return R()

    r = PublicReader("x.circle.so", session=Sess(), requests_per_minute=100000)
    ok, recs = r.read_space(1, since=now - timedelta(days=30))
    ids = [x["id"] for x in recs]
    assert ids == [1]  # stopped at the first post older than the cutoff


def test_read_space_no_cutoff_reads_all():
    from circle_leads.scraper.public_reader import PublicReader
    page = {"records": [{"id": 1, "created_at": "2020-01-01T00:00:00Z"}],
            "has_next_page": False}

    class Sess:
        def get(self, url, **kw):
            class R:
                status_code = 200
                def json(self): return page
            return R()

    r = PublicReader("x.circle.so", session=Sess(), requests_per_minute=100000)
    ok, recs = r.read_space(1)  # no since -> old post still read
    assert len(recs) == 1
