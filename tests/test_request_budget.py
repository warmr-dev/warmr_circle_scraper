"""Circle's per-IP budget: one shared governor, and fewer requests per result."""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import pytest
import requests
from sqlalchemy import select

from circle_leads.scraper import governor as gov
from circle_leads.scraper import http_client
from circle_leads.storage.database import Database, get_or_create_community
from circle_leads.storage.models import Community, Post


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _write_limits(path, **limits):
    import json

    path.write_text(json.dumps({"limits": {**gov.DEFAULT_LIMITS, **limits}}))


# --- governor ---------------------------------------------------------------


def test_two_processes_share_one_pace(tmp_path):
    """Two governors on one file (= two processes on one Mac) share the pace."""
    path = tmp_path / "gov.json"
    _write_limits(path, requestsPerMinute=600)  # 100 ms apart
    a, b = gov.CircleGovernor(path), gov.CircleGovernor(path)

    start = time.monotonic()
    for g in (a, b, a, b):
        g.acquire()
    # Four requests at 100 ms spacing: the last starts >= 300 ms after the first.
    assert time.monotonic() - start >= 0.29


def test_a_trip_pauses_every_process(tmp_path):
    path = tmp_path / "gov.json"
    a, b = gov.CircleGovernor(path), gov.CircleGovernor(path)
    a.trip("HTTP 429 on x.circle.so")
    with pytest.raises(gov.CircleRateLimited):
        b.acquire(max_wait_s=0)
    assert b.status()["cooldownReason"] == "HTTP 429 on x.circle.so"


def test_low_priority_gets_every_other_slot_while_reads_run(tmp_path):
    path = tmp_path / "gov.json"
    _write_limits(path, requestsPerMinute=6000)  # 10 ms apart
    g = gov.CircleGovernor(path)
    g.acquire()  # a high-priority read just ran
    first = time.time() + g._reserve(gov.LOW).wait_s
    second = time.time() + g._reserve(gov.LOW).wait_s
    # Low requests are spaced at twice the interval: >= 20 ms between them.
    assert second - first >= 0.019


def test_low_priority_runs_at_full_pace_when_alone(tmp_path):
    path = tmp_path / "gov.json"
    _write_limits(path, requestsPerMinute=6000)
    g = gov.CircleGovernor(path)
    first = time.time() + g._reserve(gov.LOW).wait_s
    second = time.time() + g._reserve(gov.LOW).wait_s
    assert second - first < 0.015


def test_hourly_ceiling(tmp_path):
    path = tmp_path / "gov.json"
    _write_limits(path, requestsPerMinute=60000, maxPerHour=3)
    g = gov.CircleGovernor(path)
    for _ in range(3):
        assert g._reserve(gov.HIGH).wait_s < 1
    # The fourth waits for the first to age out of the hour.
    assert g._reserve(gov.HIGH).wait_s > 3000


def test_unusable_file_falls_back_to_a_process_budget(tmp_path):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("")
    g = gov.CircleGovernor(blocker / "gov.json")
    g.acquire()
    g.trip("test")
    with pytest.raises(gov.CircleRateLimited):
        g.acquire(max_wait_s=0)


@pytest.mark.parametrize("host,expected", [
    ("pub.circle.so", True),
    ("forum.joelpilger.com", True),
    ("openrouter.ai", False),
    ("qxpewmsujqtbwhpddlti.supabase.co", False),
    ("api.exa.ai", False),
    ("", False),
])
def test_is_circle_host(host, expected):
    assert gov.is_circle_host(host) is expected


# --- adapter ----------------------------------------------------------------


class _Resp:
    def __init__(self, status, headers=None):
        self.status_code = status
        self.headers = headers or {}


@pytest.fixture
def live_governor(tmp_path, monkeypatch):
    monkeypatch.setenv("CIRCLE_GOVERNOR", "on")
    monkeypatch.setenv("CIRCLE_GOVERNOR_FILE", str(tmp_path / "gov.json"))
    monkeypatch.setattr(http_client, "_canary_checked_at", 0.0)
    return gov.get_governor()


def _adapter_with(monkeypatch, *, reply, canary):
    sent = []

    def fake_send(self, request, **kw):
        sent.append(request.url)
        return canary if request.url == http_client.DEFAULT_CANARY else reply

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", fake_send)
    return http_client.GovernedAdapter(), sent


def _prepared(url):
    return requests.Request("GET", url).prepare()


def test_429_trips_the_budget_when_the_canary_is_refused_too(live_governor, monkeypatch):
    adapter, _ = _adapter_with(monkeypatch, reply=_Resp(429), canary=_Resp(429))
    adapter.send(_prepared("https://x.circle.so/internal_api/spaces"))
    assert live_governor.status()["cooldownUntil"] is not None


def test_one_hosts_challenge_does_not_pause_everything(live_governor, monkeypatch):
    """A challenge on one host (e.g. a replayed cookie on Railway) while the
    canary answers normally is that host's problem, not an IP block."""
    adapter, _ = _adapter_with(
        monkeypatch, reply=_Resp(403, {"cf-mitigated": "challenge"}), canary=_Resp(200)
    )
    adapter.send(_prepared("https://x.circle.so/internal_api/spaces"))
    assert live_governor.status()["cooldownUntil"] is None


def test_non_circle_hosts_skip_the_budget(live_governor, monkeypatch):
    live_governor.trip("test")
    adapter, sent = _adapter_with(monkeypatch, reply=_Resp(200), canary=_Resp(200))
    monkeypatch.setenv("CIRCLE_GOVERNOR_MAX_WAIT", "0")
    adapter.send(_prepared("https://openrouter.ai/api/v1/chat/completions"))
    assert sent == ["https://openrouter.ai/api/v1/chat/completions"]
    with pytest.raises(gov.CircleRateLimited):
        adapter.send(_prepared("https://x.circle.so/internal_api/spaces"))


def test_off_by_default_on_vercel(monkeypatch):
    monkeypatch.delenv("CIRCLE_GOVERNOR", raising=False)
    monkeypatch.setenv("VERCEL", "1")
    assert gov.enabled() is False
    monkeypatch.delenv("VERCEL")
    assert gov.enabled() is True


def test_retries_never_repeat_a_429():
    retry = http_client._retry()
    assert 429 not in retry.status_forcelist
    assert retry.total <= 1 and retry.other == 0


# --- harvest: fewer requests per community ----------------------------------


class _Space:
    def __init__(self, sid, name):
        self.id, self.slug, self.name, self.is_public = sid, name.lower(), name, None


class _Reader:
    def __init__(self, host, **kw):
        self.base = f"https://{host}"
        self.session = object()
        self.name_calls = 0

    def list_spaces(self):
        return [_Space("1", "Jobs")]

    def read_space(self, sid, **kw):
        return True, []

    def community_name(self):
        self.name_calls += 1
        return "Asked Twice"


@pytest.fixture
def hdb(tmp_path):
    d = Database(f"sqlite:///{tmp_path}/budget.db")
    with d.session() as s:
        c = get_or_create_community(s, slug="pub", url="https://pub.circle.so")
        c.platform = "circle"
    return d


def _harvest(db, monkeypatch, **kw):
    import circle_leads.harvest as h
    from circle_leads.discovery.join_type import JoinClassification

    readers, joins = [], []

    def make_reader(host, **k):
        r = _Reader(host)
        readers.append(r)
        return r

    def join(host, session=None):
        joins.append(host)
        return JoinClassification("free_join", "stub", name="Pub Community")

    monkeypatch.setattr(h, "PublicReader", make_reader)
    monkeypatch.setattr(h, "fetch_join_classification", join)
    h.harvest(db, h.Requirements(), search=False, **kw)
    return readers, joins


def test_name_comes_from_the_join_call_not_a_second_request(hdb, monkeypatch):
    readers, joins = _harvest(hdb, monkeypatch)
    assert joins == ["pub.circle.so"]
    assert readers[0].name_calls == 0
    with hdb.session() as s:
        assert s.scalar(select(Community)).name == "Pub Community"


def test_join_type_is_not_refetched_within_a_week(hdb, monkeypatch):
    with hdb.session() as s:
        c = s.scalar(select(Community))
        c.name, c.join_type = "Pub Community", "free_join"
        c.join_type_checked_at = _now() - timedelta(days=2)
        c.last_synced_at = _now() - timedelta(days=30)
    readers, joins = _harvest(hdb, monkeypatch)
    assert len(readers) == 1 and joins == []


def _set(db, **fields):
    with db.session() as s:
        c = s.scalar(select(Community))
        for k, v in fields.items():
            setattr(c, k, v)
        return c.id


def _post(db, days_ago):
    with db.session() as s:
        c = s.scalar(select(Community))
        s.add(Post(community_id=c.id, source_content_id=f"p{days_ago}", content="x",
                   dedup_hash=f"h{days_ago}", published_at=_now() - timedelta(days=days_ago)))


def test_private_community_waits_a_week(hdb, monkeypatch):
    _set(hdb, last_synced_at=_now() - timedelta(days=3), read_outcome="private")
    readers, _ = _harvest(hdb, monkeypatch, min_recheck_hours=6)
    assert readers == []


def test_gone_community_waits_a_week(hdb, monkeypatch):
    _set(hdb, last_synced_at=_now() - timedelta(days=3), read_outcome="gone")
    readers, _ = _harvest(hdb, monkeypatch, min_recheck_hours=6)
    assert readers == []


def test_a_failed_read_is_retried_on_the_normal_interval(hdb, monkeypatch):
    """A timeout / 429 / 5xx says nothing about the community -- it must not
    park it for a week the way a confirmed private one is."""
    _set(hdb, last_synced_at=_now() - timedelta(hours=7), read_outcome="error")
    readers, _ = _harvest(hdb, monkeypatch, min_recheck_hours=6)
    assert len(readers) == 1


def test_rows_read_before_the_column_keep_the_old_interval(hdb, monkeypatch):
    _set(hdb, last_synced_at=_now() - timedelta(hours=7), read_outcome=None)
    readers, _ = _harvest(hdb, monkeypatch, min_recheck_hours=6)
    assert len(readers) == 1


def test_public_with_no_posts_is_quiet_not_closed(hdb, monkeypatch):
    """Public spaces with nothing stored (empty hiring spaces, or every post
    past the age cap) wait 72 h, not a week."""
    _set(hdb, last_synced_at=_now() - timedelta(hours=30), read_outcome="public")
    assert _harvest(hdb, monkeypatch, min_recheck_hours=6)[0] == []
    _set(hdb, last_synced_at=_now() - timedelta(hours=73))
    assert len(_harvest(hdb, monkeypatch, min_recheck_hours=6)[0]) == 1


def test_active_community_keeps_the_short_interval(hdb, monkeypatch):
    _set(hdb, last_synced_at=_now() - timedelta(hours=7), read_outcome="public")
    _post(hdb, days_ago=2)
    readers, _ = _harvest(hdb, monkeypatch, min_recheck_hours=6)
    assert len(readers) == 1


def test_quiet_community_waits_three_days(hdb, monkeypatch):
    _set(hdb, last_synced_at=_now() - timedelta(hours=30), read_outcome="public")
    _post(hdb, days_ago=90)
    readers, _ = _harvest(hdb, monkeypatch, min_recheck_hours=6)
    assert readers == []


@pytest.mark.parametrize("status,has_spaces,join_detail,expected", [
    (200, True, "", "public"),
    (200, False, "", "public"),        # list answered, just empty
    (401, False, "", "private"),
    (403, False, "", "private"),
    (404, False, "", "gone"),
    (401, False, "host no longer maps to a community (redirects to circle.so marketing site)", "gone"),
    (0, False, "", "error"),           # timeout, TLS, DNS
    (429, False, "", "error"),
    (503, False, "", "error"),
])
def test_list_outcome(status, has_spaces, join_detail, expected):
    from circle_leads.discovery.join_type import JoinClassification
    from circle_leads.harvest import _list_outcome

    join = JoinClassification("unknown", join_detail) if join_detail else None
    assert _list_outcome(status, has_spaces, join) == expected


def test_harvest_records_the_outcome(hdb, monkeypatch):
    import circle_leads.harvest as h

    class Locked(_Reader):
        last_status = 401

        def list_spaces(self):
            return []

    class Flaky(_Reader):
        last_status = 0

        def list_spaces(self):
            return []

    for reader, expected in ((Locked, "private"), (Flaky, "error"), (_Reader, "public")):
        _set(hdb, last_synced_at=None, read_outcome=None)
        monkeypatch.setattr(h, "PublicReader", reader)
        monkeypatch.setattr(h, "fetch_join_classification", lambda host, session=None: None)
        h.harvest(hdb, h.Requirements(), search=False)
        with hdb.session() as s:
            assert s.scalar(select(Community)).read_outcome == expected, reader.__name__


def test_force_recheck_overrides_the_backoff(hdb, monkeypatch):
    with hdb.session() as s:
        s.scalar(select(Community)).last_synced_at = _now() - timedelta(hours=1)
    readers, _ = _harvest(hdb, monkeypatch, force_recheck=True)
    assert len(readers) == 1


def test_directory_listing_is_never_read(tmp_path, monkeypatch):
    d = Database(f"sqlite:///{tmp_path}/dir.db")
    with d.session() as s:
        c = get_or_create_community(
            s, slug="listing", url="https://discover.circle.so/products/some-community"
        )
        c.platform = "circle"
    readers, _ = _harvest(d, monkeypatch)
    assert readers == []


# --- enrichment -------------------------------------------------------------


class _EnrichReader:
    def __init__(self, status, payload=None):
        self.last_status, self.last_payload = status, payload

    def __call__(self, host, session=None):
        return self

    def community_name(self):
        if self.last_status == 200 and isinstance(self.last_payload, dict):
            return self.last_payload.get("name")
        return None


def test_unreachable_host_skips_the_landing_page(monkeypatch):
    from circle_leads import pipeline

    monkeypatch.setattr(pipeline, "PublicReader", _EnrichReader(0))

    def must_not_run(url, session=None):
        raise AssertionError("landing page fetched for a dead host")

    monkeypatch.setattr(pipeline, "check_public_access", must_not_run)
    meta = pipeline.fetch_community_metadata("dead.example.com", "https://dead.example.com")
    assert meta.name is None and meta.note == "host unreachable"


def test_enrichment_stores_join_type_from_the_same_response(tmp_path, monkeypatch):
    from circle_leads import pipeline
    from circle_leads.discovery.validate_community import AccessCheck

    payload = {"name": "Acme Founders", "is_private": False,
               "allow_signups_to_public_community": True}
    monkeypatch.setattr(pipeline, "PublicReader", _EnrichReader(200, payload))
    monkeypatch.setattr(pipeline, "check_public_access",
                        lambda url, session=None: AccessCheck(description="Founders building SaaS"))
    d = Database(f"sqlite:///{tmp_path}/enrich.db")
    with d.session() as s:
        get_or_create_community(s, slug="acme", url="https://acme.circle.so")

    stats = pipeline.enrich_pending(d, limit=5)
    assert stats["join_typed"] == 1
    with d.session() as s:
        c = s.scalar(select(Community))
        assert (c.name, c.join_type) == ("Acme Founders", "free_join")
        assert c.join_type_checked_at is not None
