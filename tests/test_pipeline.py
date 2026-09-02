"""End-to-end pipeline test against a mocked Circle API.

No network calls: a fake transport returns realistic Admin API v2 payloads,
including the page/per_page/has_next_page envelope.
"""

import pytest

from circle_leads.config.settings import CommunityPermission, load_requirements
from circle_leads.export.exporters import query_leads, to_csv, to_json
from circle_leads.pipeline import classify_pending, ingest_community
from circle_leads.storage.database import Database

SPACES = [
    {"id": 1, "name": "General Discussion", "slug": "general"},
    {"id": 2, "name": "Off Topic", "slug": "off-topic"},
]

POSTS = {
    "1": [
        {
            "id": 101, "space_id": 1, "name": "Hiring a backend engineer",
            "body": {"body": "<p>We are looking for a senior backend developer to build our API. "
                             "Python and PostgreSQL required. Budget $15,000. Remote, start ASAP.</p>"},
            "url": "/c/general/hiring-backend", "published_at": "2026-08-30T10:00:00Z",
            "user": {"id": 55, "name": "Dana Ops", "url": "/u/dana"},
        },
        {
            "id": 102, "space_id": 1, "name": "Looking for work",
            "body": {"body": "<p>I am a Flutter developer looking for a job. Open to work, "
                             "remote preferred. My portfolio is in my profile.</p>"},
            "url": "/c/general/looking-for-work", "published_at": "2026-08-29T10:00:00Z",
            "user": {"id": 56, "name": "Sam Dev", "url": "/u/sam"},
        },
        {
            "id": 103, "space_id": 1, "name": "Role filled",
            "body": {"body": "<p>Thanks all, we are not hiring for this role anymore.</p>"},
            "url": "/c/general/filled", "published_at": "2026-08-28T10:00:00Z",
            "user": {"id": 55, "name": "Dana Ops"},
        },
    ]
}

COMMENTS = {
    "101": [
        {"id": 9001, "post_id": 101, "body": {"body": "Sounds great, sending you a DM."},
         "created_at": "2026-08-30T11:00:00Z", "user": {"id": 57, "name": "Ali"}},
    ]
}


class FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status
        self.headers = {}
        self.text = str(payload)

    def json(self):
        return self._payload


class FakeSession:
    """Minimal stand-in for requests.Session covering the endpoints we call."""

    def __init__(self):
        self.calls = []

    def request(self, method, url, headers=None, timeout=None, params=None, **kw):
        self.calls.append((method, url, params))
        assert headers and "Authorization" in headers

        if url.endswith("/api/admin/v2/spaces"):
            return FakeResponse(self._envelope(SPACES))
        if url.endswith("/api/admin/v2/posts"):
            space_id = str((params or {}).get("space_id"))
            return FakeResponse(self._envelope(POSTS.get(space_id, [])))
        if url.endswith("/api/admin/v2/comments"):
            post_id = str((params or {}).get("post_id"))
            return FakeResponse(self._envelope(COMMENTS.get(post_id, [])))
        return FakeResponse({"records": [], "has_next_page": False}, 200)

    @staticmethod
    def _envelope(records):
        return {
            "page": 1, "per_page": 100, "has_next_page": False,
            "count": len(records), "page_count": 1, "records": records,
        }


@pytest.fixture
def approved_permission(monkeypatch):
    monkeypatch.setenv("CIRCLE_TEST_ADMIN_TOKEN", "test-token-not-real")
    return CommunityPermission(
        community_id="acme-founders",
        community_url="https://acme-founders.circle.so",
        permission_status="approved",
        ingestion_route="admin_api_v2",
        allowed_space_ids=["1"],          # space 2 is deliberately NOT approved
        admin_token_env="CIRCLE_TEST_ADMIN_TOKEN",
        approval_reference="Email 2026-09-01",
    )


@pytest.fixture
def patched_client(monkeypatch):
    fake = FakeSession()
    import circle_leads.scraper.pagination as pagination

    original = pagination.CircleClient.__init__

    def patched(self, *args, **kwargs):
        kwargs["session"] = fake
        kwargs["requests_per_minute"] = 100000  # no sleeping in tests
        original(self, *args, **kwargs)

    monkeypatch.setattr(pagination.CircleClient, "__init__", patched)
    return fake


def test_full_pipeline_ingest_classify_export(
    tmp_path, approved_permission, patched_client
):
    db = Database(f"sqlite:///{tmp_path}/pipeline.db")
    reqs = load_requirements()

    summary = ingest_community(db, approved_permission, reqs)
    assert summary.state == "COMPLETE", summary.errors
    # 3 posts from the approved space + 1 comment; nothing from space 2.
    assert summary.items_seen == 4
    assert summary.items_new == 4

    stats = classify_pending(db, reqs, use_llm=False)
    assert stats["classified"] == 4
    assert stats["leads"] == 1          # only the backend-engineer post
    assert stats["not_leads"] >= 2      # job seeker + "not hiring"

    with db.session() as s:
        rows = query_leads(s)
    assert len(rows) == 1
    lead = rows[0]
    assert lead["community"] == "acme-founders"
    assert lead["classification"] == "LEAD"
    assert lead["priority"] == "HIGH"
    assert "Python" in lead["skills"]
    assert lead["url"].startswith("https://acme-founders.circle.so/")
    assert lead["permission_reference"] == "Email 2026-09-01"

    csv_path = to_csv(rows, tmp_path / "leads.csv")
    body = csv_path.read_text()
    assert "community,author,content" in body
    assert "acme-founders" in body

    json_path = to_json(rows, tmp_path / "leads.json")
    import json
    assert json.loads(json_path.read_text())[0]["lead_score"] == lead["lead_score"]


def test_unapproved_space_is_never_requested(
    tmp_path, approved_permission, patched_client
):
    db = Database(f"sqlite:///{tmp_path}/p.db")
    ingest_community(db, approved_permission, load_requirements())
    requested = [
        str((p or {}).get("space_id"))
        for m, u, p in patched_client.calls
        if u.endswith("/api/admin/v2/posts")
    ]
    assert "2" not in requested
    assert requested == ["1"]


def test_second_run_is_incremental(tmp_path, approved_permission, patched_client):
    db = Database(f"sqlite:///{tmp_path}/p.db")
    reqs = load_requirements()
    first = ingest_community(db, approved_permission, reqs)
    second = ingest_community(db, approved_permission, reqs)

    assert first.items_new == 4
    # Everything is already stored, so a re-run adds nothing.
    assert second.items_new == 0
    assert second.items_updated == 0


def test_classification_is_not_repeated(tmp_path, approved_permission, patched_client):
    db = Database(f"sqlite:///{tmp_path}/p.db")
    reqs = load_requirements()
    ingest_community(db, approved_permission, reqs)
    classify_pending(db, reqs)
    again = classify_pending(db, reqs)
    assert again["classified"] == 0
