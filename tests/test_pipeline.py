"""End-to-end pipeline test against a mocked Circle API.

No network calls: a fake transport returns realistic Admin API v2 payloads,
including the page/per_page/has_next_page envelope. The enrichment and ICP
re-score sections further down stub the two public readers the same way -- they
must never reach a real host, and a test that quietly did would look identical
to one that didn't.
"""

import threading
import time
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import select

from circle_leads.classifier.icp_relevance import LLM_FIT_NEEDS_REVIEW
from circle_leads.config.settings import CommunityPermission
from circle_leads.config.settings import load_requirements as _load_requirements
from circle_leads.discovery.validate_community import AccessCheck
from circle_leads.export.exporters import query_leads, to_csv, to_json
from circle_leads.pipeline import (
    ENRICH_MAX_WORKERS,
    CommunityMetadata,
    _clean_name,
    _is_boilerplate,
    _llm_eligible_icp_ids,
    ICP_SCHEDULED_BATCH,
    classify_icp_pending,
    classify_pending,
    enrich_pending,
    fetch_community_metadata,
    ingest_community,
)
from circle_leads.storage.database import Database, get_or_create_community
from circle_leads.storage.models import Community

# Use the stable developer-targeting test config, not the live product config
# (which has been retargeted to founders/CEOs). Keeps these tests asserting
# classifier/pipeline behaviour rather than the product's current aim.
_DEV_CONFIG = Path(__file__).parent / "fixtures" / "dev_requirements.yaml"


def load_requirements():
    return _load_requirements(str(_DEV_CONFIG))

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


# --- ICP re-score selection --------------------------------------------------
#
# The scheduled sweep's default selection (icp_checked_at IS NULL) is a no-op on
# a database where every row has already been stamped once -- which is every
# production row. These cover the way out of that: re-score the rows an LLM
# could still change its mind about, and only those.


class StubIcpBackend:
    """Minimal LlmBackend stand-in; answers every escalation the same way."""

    model = "stub-model"

    def __init__(self, fit=True, confidence=0.9):
        self.calls = 0
        self._payload = {
            "fit": fit, "confidence": confidence,
            "reason": "stub", "signals": ["stub_signal"],
        }

    def complete(self, system, user):
        self.calls += 1
        import json
        return json.dumps(self._payload)


def _icp_db(tmp_path, rows):
    """Build a DB of communities from (slug, kwargs) pairs, in order."""
    db = Database(f"sqlite:///{tmp_path}/icp.db")
    with db.session() as s:
        for slug, kw in rows:
            get_or_create_community(s, slug=slug, url=f"https://{slug}.circle.so", **kw)
    return db


def _slugs_for(db, ids):
    with db.session() as s:
        return [s.get(Community, i).slug for i in ids]


BAND_TEXT = {  # scores 0 with the rules: ambiguous, so it escalates
    "name": "The Tuesday Group",
    "description": "A friendly place to talk about what we are working on.",
}


def test_rescore_selects_only_rows_an_llm_could_still_change(tmp_path):
    stamped = datetime(2026, 9, 1, 12, 0, 0)
    db = _icp_db(tmp_path, [
        ("never-checked", dict(**BAND_TEXT)),
        ("rules-ambiguous", dict(**BAND_TEXT, icp_checked_at=stamped,
                                 icp_decided_by="rules", icp_flag=False)),
        ("rules-flagged", dict(**BAND_TEXT, icp_checked_at=stamped,
                               icp_decided_by="rules", icp_flag=True)),
        ("llm-decided", dict(**BAND_TEXT, icp_checked_at=stamped,
                             icp_decided_by="llm", icp_flag=False)),
        ("no-text", dict(name="", description="", icp_checked_at=stamped,
                         icp_decided_by="rules", icp_flag=False)),
    ])

    with db.session() as s:
        eligible = _llm_eligible_icp_ids(s, limit=50)

    # The never-checked row is the *other* selection's job; a flagged row is
    # already in the join queue and must not be demoted by a stub opinion; an
    # llm-decided row has had its LLM call; a blank row has nothing to judge.
    assert _slugs_for(db, eligible) == ["rules-ambiguous"]


def test_rescore_walks_oldest_stamp_first_and_is_bounded(tmp_path):
    db = _icp_db(tmp_path, [
        ("checked-recently", dict(**BAND_TEXT, icp_decided_by="rules",
                                  icp_checked_at=datetime(2026, 9, 16, 9, 0))),
        ("checked-long-ago", dict(**BAND_TEXT, icp_decided_by="rules",
                                  icp_checked_at=datetime(2026, 8, 1, 9, 0))),
    ])

    with db.session() as s:
        assert _slugs_for(db, _llm_eligible_icp_ids(s, limit=1)) == ["checked-long-ago"]
        assert _slugs_for(db, _llm_eligible_icp_ids(s, limit=50)) == [
            "checked-long-ago", "checked-recently",
        ]


def test_scheduled_rescore_reaches_already_stamped_rows(tmp_path, monkeypatch):
    """The whole point: with every row stamped, the default sweep does nothing
    and the LLM never gets to see a single community."""
    db = _icp_db(tmp_path, [
        ("rules-ambiguous", dict(**BAND_TEXT, icp_decided_by="rules",
                                 icp_checked_at=datetime(2026, 9, 1, 12, 0))),
    ])
    backend = StubIcpBackend()
    monkeypatch.setattr("circle_leads.pipeline.make_backend", lambda: backend)
    reqs = load_requirements()

    assert classify_icp_pending(db, reqs, use_llm=True)["checked"] == 0

    stats = classify_icp_pending(db, reqs, use_llm=True, rescore_llm_eligible=True)
    assert stats["checked"] == 1
    assert backend.calls == 1
    with db.session() as s:
        row = s.scalar(select(Community).where(Community.slug == "rules-ambiguous"))
        assert row.icp_decided_by == "llm"
        # Recorded, not enqueued: the scheduled sweep is unattended, so an LLM
        # fit waits for review instead of reaching the auto-join bot.
        assert row.icp_flag is False
        assert LLM_FIT_NEEDS_REVIEW in row.icp_reasons


def test_scheduled_rescore_does_not_grow_the_join_queue(tmp_path, monkeypatch):
    """icp_flag is the only gate in front of join/joiner.py, which drives a
    browser on the user's real Circle account. An unattended tick may fill the
    review backlog; it may not fill the join queue."""
    db = _icp_db(tmp_path, [
        ("rules-ambiguous", dict(**BAND_TEXT, icp_decided_by="rules",
                                 icp_checked_at=datetime(2026, 9, 1, 12, 0))),
    ])
    monkeypatch.setattr("circle_leads.pipeline.make_backend",
                        lambda: StubIcpBackend(fit=True, confidence=0.95))

    stats = classify_icp_pending(
        db, load_requirements(), use_llm=True, rescore_llm_eligible=True
    )
    assert stats["flagged"] == 0
    assert stats["not_flagged"] == 1


def test_trust_llm_flags_lets_an_llm_fit_into_the_join_queue(tmp_path, monkeypatch):
    """The opt-in a human types (filter-relevant --trust-llm-flags); the worker
    never passes it."""
    db = _icp_db(tmp_path, [
        ("rules-ambiguous", dict(**BAND_TEXT, icp_decided_by="rules",
                                 icp_checked_at=datetime(2026, 9, 1, 12, 0))),
    ])
    monkeypatch.setattr("circle_leads.pipeline.make_backend",
                        lambda: StubIcpBackend(fit=True, confidence=0.95))

    stats = classify_icp_pending(
        db, load_requirements(), use_llm=True, rescore_llm_eligible=True,
        trust_llm_flags=True,
    )
    assert stats["flagged"] == 1
    with db.session() as s:
        row = s.scalar(select(Community).where(Community.slug == "rules-ambiguous"))
        assert row.icp_flag is True


def test_scheduled_sweep_bounds_the_never_checked_branch_too(tmp_path, monkeypatch):
    """The re-score branch was capped from the start; the never-checked branch
    was not, so one directory crawl could hand a single tick thousands of rows
    -- each an LLM call. The scheduled path caps both together."""
    db = _icp_db(tmp_path, [
        (f"new-{i}", dict(**BAND_TEXT)) for i in range(ICP_SCHEDULED_BATCH + 25)
    ])
    backend = StubIcpBackend(fit=False)
    monkeypatch.setattr("circle_leads.pipeline.make_backend", lambda: backend)

    stats = classify_icp_pending(
        db, load_requirements(), use_llm=True, rescore_llm_eligible=True
    )
    assert stats["checked"] == ICP_SCHEDULED_BATCH
    assert backend.calls == ICP_SCHEDULED_BATCH

    # An explicit limit still wins -- the cap is a default, not a ceiling on
    # what a human can ask for.
    backend.calls = 0
    stats = classify_icp_pending(
        db, load_requirements(), use_llm=True, rescore_llm_eligible=True, limit=3
    )
    assert stats["checked"] == 3


def test_rescore_is_skipped_without_an_llm_backend(tmp_path, monkeypatch):
    """No key, no point: re-running identical rules over identical text would
    rewrite the identical verdict and churn icp_checked_at for nothing."""
    db = _icp_db(tmp_path, [
        ("rules-ambiguous", dict(**BAND_TEXT, icp_decided_by="rules",
                                 icp_checked_at=datetime(2026, 9, 1, 12, 0))),
    ])
    monkeypatch.setattr("circle_leads.pipeline.make_backend", lambda: None)

    stats = classify_icp_pending(
        db, load_requirements(), use_llm=True, rescore_llm_eligible=True
    )
    assert stats["checked"] == 0


def test_rescore_batch_is_bounded_per_run(tmp_path, monkeypatch):
    db = _icp_db(tmp_path, [
        (f"amb-{i}", dict(**BAND_TEXT, icp_decided_by="rules",
                          icp_checked_at=datetime(2026, 9, 1, 12, i)))
        for i in range(5)
    ])
    backend = StubIcpBackend(fit=False)
    monkeypatch.setattr("circle_leads.pipeline.make_backend", lambda: backend)

    stats = classify_icp_pending(
        db, load_requirements(), use_llm=True,
        rescore_llm_eligible=True, rescore_limit=2,
    )
    assert stats["checked"] == 2
    assert backend.calls == 2


def test_never_checked_rows_keep_priority_over_rescores(tmp_path, monkeypatch):
    db = _icp_db(tmp_path, [
        ("rules-ambiguous", dict(**BAND_TEXT, icp_decided_by="rules",
                                 icp_checked_at=datetime(2026, 8, 1, 12, 0))),
        ("never-checked", dict(**BAND_TEXT)),
    ])
    monkeypatch.setattr("circle_leads.pipeline.make_backend", lambda: StubIcpBackend())

    stats = classify_icp_pending(
        db, load_requirements(), use_llm=True, rescore_llm_eligible=True, limit=1
    )
    assert stats["checked"] == 1
    with db.session() as s:
        assert s.scalar(
            select(Community).where(Community.slug == "never-checked")
        ).icp_checked_at is not None


# --- Enrichment --------------------------------------------------------------


class RecordingFetch:
    """Stand-in for fetch_community_metadata that records what it was asked."""

    def __init__(self, result=None, delay=0.0):
        self.result = result or CommunityMetadata(name="Fetched Name")
        self.delay = delay
        self.calls = []
        self._lock = threading.Lock()
        self._live = 0
        self.peak_concurrency = 0

    def __call__(self, host, url, *, session=None, want_name=True, want_description=True):
        with self._lock:
            self.calls.append((host, want_name, want_description))
            self._live += 1
            self.peak_concurrency = max(self.peak_concurrency, self._live)
        try:
            if self.delay:
                time.sleep(self.delay)
            return self.result
        finally:
            with self._lock:
                self._live -= 1


def _blank_communities(tmp_path, count, name="", description=""):
    db = Database(f"sqlite:///{tmp_path}/enrich.db")
    with db.session() as s:
        for i in range(count):
            get_or_create_community(
                s, slug=f"c{i}", url=f"https://c{i}.circle.so",
                name=name, description=description,
            )
    return db


def test_enrichment_walks_the_backlog_oldest_first(tmp_path):
    db = _blank_communities(tmp_path, 5)
    fetch = RecordingFetch()

    first = enrich_pending(db, limit=2, fetch=fetch)
    assert [c[0] for c in fetch.calls] == ["c0.circle.so", "c1.circle.so"]
    assert first["visited"] == 2
    assert first["named"] == 2

    # Second run resumes where the first stopped -- it does not re-read the
    # head of the table the way harvest's (watching, relevance_score) window
    # does, which is the entire reason this stage exists.
    second = enrich_pending(db, limit=2, fetch=fetch)
    assert [c[0] for c in fetch.calls[2:]] == ["c2.circle.so", "c3.circle.so"]
    assert second["cursor"] > first["cursor"]


def test_enrichment_wraps_when_the_backlog_is_exhausted(tmp_path):
    db = _blank_communities(tmp_path, 2)
    first = enrich_pending(db, limit=5, fetch=RecordingFetch())
    assert first["visited"] == 2

    # Nothing left past the cursor: the lap ends and the cursor goes back to
    # the start rather than parking at the end of the table forever.
    end_of_lap = enrich_pending(db, limit=5, fetch=RecordingFetch())
    assert end_of_lap["visited"] == 0
    assert end_of_lap["wrapped"] == 1
    assert end_of_lap["cursor"] == 0

    # Both rows still lack a description, so the next lap has real work again --
    # which is how a host that was down (or bot-checked) gets another chance.
    second_lap = enrich_pending(db, limit=5, fetch=RecordingFetch())
    assert second_lap["visited"] == 2
    assert second_lap["wrapped"] == 0


def test_enrichment_caps_concurrency_at_four(tmp_path):
    """Measured: at 12 threads, 72% of *.circle.so responses came back silently
    empty. A caller asking for 12 must still get at most ENRICH_MAX_WORKERS."""
    db = _blank_communities(tmp_path, 12)
    fetch = RecordingFetch(delay=0.02)

    enrich_pending(db, limit=12, max_workers=12, fetch=fetch)

    assert fetch.peak_concurrency <= ENRICH_MAX_WORKERS
    assert len(fetch.calls) == 12


def test_enrichment_only_asks_for_the_fields_that_are_missing(tmp_path):
    db = _blank_communities(tmp_path, 1, name="Already Named")
    fetch = RecordingFetch(result=CommunityMetadata(description="A real description."))

    stats = enrich_pending(db, limit=1, fetch=fetch)

    assert fetch.calls == [("c0.circle.so", False, True)]
    assert stats["described"] == 1
    with db.session() as s:
        row = s.scalar(select(Community).where(Community.slug == "c0"))
        assert row.name == "Already Named"
        assert row.description == "A real description."


def test_enrichment_does_not_store_rejected_boilerplate(tmp_path):
    db = _blank_communities(tmp_path, 1)
    fetch = RecordingFetch(
        result=CommunityMetadata(name="Acme", rejected=["description"])
    )

    stats = enrich_pending(db, limit=1, fetch=fetch)

    assert stats["boilerplate_rejected"] == 1
    assert stats["described"] == 0
    with db.session() as s:
        assert not s.scalar(
            select(Community).where(Community.slug == "c0")
        ).description


def test_enrichment_skips_directory_listings_but_walks_past_them(tmp_path):
    db = Database(f"sqlite:///{tmp_path}/skip.db")
    with db.session() as s:
        get_or_create_community(
            s, slug="listing-1",
            url="https://discover.circle.so/c/listing-1", name="", description="",
        )
        get_or_create_community(
            s, slug="real", url="https://real.circle.so", name="", description="",
        )
    fetch = RecordingFetch()

    stats = enrich_pending(db, limit=5, fetch=fetch)

    # A discover.circle.so row is a marketplace card, not a community host:
    # fetching it returns the directory page, never this community's metadata.
    assert [c[0] for c in fetch.calls] == ["real.circle.so"]
    assert stats["skipped"] == 1


def test_a_dead_host_does_not_end_the_pass(tmp_path):
    db = _blank_communities(tmp_path, 3)
    seen = []

    def exploding(host, url, *, session=None, want_name=True, want_description=True):
        seen.append(host)
        if host == "c1.circle.so":
            raise RuntimeError("connection reset")
        return CommunityMetadata(name="Fetched Name")

    stats = enrich_pending(db, limit=3, max_workers=1, fetch=exploding)

    assert len(seen) == 3
    assert stats["named"] == 2
    assert stats["nothing_found"] == 1


# --- Boilerplate detection ---------------------------------------------------


@pytest.mark.parametrize("text", [
    "Explore Members space in Acme Founders",
    # The same template with the space name preceded by an article.
    "Explore the General space in Acme Founders",
    "Acme Founders community home page",
    "Acme Founders community homepage",
    "Powered by Circle",
    "Just a moment...",
    "Verifying you are human. This may take a few seconds.",
    "Attention Required! | Cloudflare",
    "Sign in",
    "Circle",
    "",
    "   ",
    # Circle's auth-page meta description, caught live on the first prod
    # enrichment run: it names the community but describes nothing, and
    # storing it would close the row to any later enrichment attempt.
    "Login to VentureRise community via email or SSO today.",
    "Log in to Acme community via SSO today.",
    "Sign up to the Foo community",
    "Create an account or log in to continue",
    # circle.so's marketing title: what a host with no community behind it
    # serves. 738 DNS rows stored it as their name on 2026-09-18.
    "Circle | A new era for digital businesses",
    "Circle - A new era for digital businesses",
])
def test_circle_boilerplate_is_rejected(text):
    assert _is_boilerplate(text) is True


@pytest.mark.parametrize("text", [
    "A community for indie SaaS founders shipping their first product.",
    # Real descriptions that merely contain the template's words. Each of these
    # was rejected while the patterns ran unanchored -- and a false positive
    # here is permanent: the description is dropped, the row still looks empty,
    # and the next enrichment pass fetches and drops the same text again.
    "Explore our space in Berlin for creative entrepreneurs.",
    "Explore the space in between design and code.",
    "Just a moment of your time each week.",
    "Attention required for founders who ship.",
    "Explore a space in your own time.",
    "Acme Founders",
    "We help agencies hire engineers without recruiters.",
    "Explore Lab: a space for builders",  # 'explore' alone is not the template
    # Guard against over-rejecting: a real description may open with the word
    # "login" without being an auth page.
    "Login flows and auth UX for product designers",
    "Sign up funnels, onboarding and activation for B2B SaaS",
])
def test_real_metadata_is_kept(text):
    assert _is_boilerplate(text) is False


class StubReader:
    """Stands in for PublicReader: only community_name() is used here."""

    def __init__(self, name):
        self._name = name
        self.built_for = []

    def __call__(self, host, session=None):
        self.built_for.append(host)
        return self

    def community_name(self):
        return self._name


def _stub_access_check(monkeypatch, **kw):
    check = AccessCheck(**kw)
    monkeypatch.setattr(
        "circle_leads.pipeline.check_public_access", lambda url, session=None: check
    )
    return check


def test_fetch_rejects_a_boilerplate_description_but_keeps_the_name(monkeypatch):
    monkeypatch.setattr("circle_leads.pipeline.PublicReader", StubReader("Acme Founders"))
    _stub_access_check(
        monkeypatch, name="Acme Founders",
        description="Explore Members space in Acme Founders",
    )

    meta = fetch_community_metadata("acme.circle.so", "https://acme.circle.so")

    # Storing the template would be worse than storing nothing: the ICP
    # classifier would score Circle's words and file the community as decided.
    assert meta.name == "Acme Founders"
    assert meta.description is None
    assert meta.rejected == ["description"]


def test_fetch_falls_back_to_the_page_title_when_the_json_name_is_junk(monkeypatch):
    monkeypatch.setattr("circle_leads.pipeline.PublicReader", StubReader("Just a moment..."))
    _stub_access_check(
        monkeypatch, name="Acme Founders",
        description="A community for indie SaaS founders.",
    )

    meta = fetch_community_metadata("acme.circle.so", "https://acme.circle.so")

    assert meta.name == "Acme Founders"
    assert meta.description == "A community for indie SaaS founders."
    assert meta.rejected == ["name"]


@pytest.mark.parametrize("raw, clean", [
    ("Log in | AI Marketer HQ (AI Agency™)", "AI Marketer HQ (AI Agency™)"),
    ("Home | B2B NEXT AI Community", "B2B NEXT AI Community"),
    ("Login &#8211; B2B eCommerce Association", "B2B eCommerce Association"),
    ("Founder&#39;s Circle", "Founder's Circle"),
    ("Ulule Connect | Ulule Connect", "Ulule Connect"),
    # Not a page prefix: real names that start with one of the words.
    ("Homebrew Founders", "Homebrew Founders"),
    ("Welcome Wagon Club", "Welcome Wagon Club"),
    ("AI Builders | Build agents with LLMs", "AI Builders | Build agents with LLMs"),
    ("", None),
    (None, None),
])
def test_clean_name(raw, clean):
    assert _clean_name(raw) == clean


def test_fetch_rejects_circles_marketing_title_as_a_name(monkeypatch):
    # A dead host redirects to circle.so, so the landing page is the marketing
    # site -- the row must stay nameless, not get "Circle | A new era ...".
    monkeypatch.setattr("circle_leads.pipeline.PublicReader", StubReader(None))
    _stub_access_check(monkeypatch, name="Circle | A new era for digital businesses")

    meta = fetch_community_metadata(
        "gone.circle.so", "https://gone.circle.so", want_description=False
    )
    assert meta.name is None
    assert meta.rejected == ["name"]


def test_fetch_strips_a_login_page_prefix_from_the_name(monkeypatch):
    monkeypatch.setattr("circle_leads.pipeline.PublicReader", StubReader(None))
    _stub_access_check(monkeypatch, name="Log in | Acme Founders")

    meta = fetch_community_metadata(
        "acme.circle.so", "https://acme.circle.so", want_description=False
    )
    assert meta.name == "Acme Founders"


def test_fetch_skips_the_landing_page_when_only_a_name_is_wanted(monkeypatch):
    monkeypatch.setattr("circle_leads.pipeline.PublicReader", StubReader("Acme Founders"))

    def _must_not_be_called(url, session=None):
        raise AssertionError("the landing page should not be fetched")

    monkeypatch.setattr("circle_leads.pipeline.check_public_access", _must_not_be_called)

    meta = fetch_community_metadata(
        "acme.circle.so", "https://acme.circle.so", want_description=False
    )
    assert meta.name == "Acme Founders"


def test_fetch_records_why_nothing_came_back(monkeypatch):
    monkeypatch.setattr("circle_leads.pipeline.PublicReader", StubReader(None))
    _stub_access_check(monkeypatch, http_status=404, note="Community not found.")

    meta = fetch_community_metadata("gone.circle.so", "https://gone.circle.so")

    assert meta.name is None and meta.description is None
    assert meta.note == "Community not found."
