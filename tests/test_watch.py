"""The fast poller: find a new post, triage it once, and never lose one.

The failures these cover are the ones the old reader actually had: a watermark
that moved on a failed read, a pinned post hiding everything below it, and the
same post arriving twice because two readers flattened it differently.
"""
from __future__ import annotations

import tempfile
from datetime import datetime, timedelta

import pytest
import requests

from circle_leads.storage.database import Database, get_or_create_community
from circle_leads.storage.models import Community, WatchMode, WatchState
from circle_leads.watch.poller import (
    WatchTuning,
    check_community,
    ensure_watch_rows,
)

HOST = "example.circle.so"


@pytest.fixture
def db():
    return Database("sqlite:///" + tempfile.mktemp(suffix=".db"))


@pytest.fixture
def community(db):
    with db.session() as s:
        c = get_or_create_community(s, slug="example", url=f"https://{HOST}",
                                    platform="circle", watching=True)
        s.flush()
        return c.id


def make_record(rid: int, *, title="We are hiring a Flutter developer",
                body="We need someone for a three month contract, budget 8k.",
                pinned=False, created="2026-09-23T13:00:00.000Z", space="jobs"):
    return {
        "id": rid,
        "name": title,
        "slug": f"post-{rid}",
        "space_slug": space,
        "truncated_content": body,
        "created_at": created,
        "published_at": created,
        "pin_to_top": pinned,
        "pinned_at_top_of_space": False,
        "community_member": {"id": 1, "name": "Ann Poster"},
    }


class FakeResponse:
    def __init__(self, status=200, payload=None, etag=None, text=""):
        self.status_code = status
        self._payload = payload
        self.headers = {"etag": etag} if etag else {}
        self.content = b"x" if status != 304 else b""
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class FakeSession:
    """Answers feed requests from a scripted list, recording what was asked."""

    def __init__(self, pages):
        self.pages = pages          # list of FakeResponse, one per call
        self.calls = []
        self.headers = {}

    def get(self, url, headers=None, cookies=None, timeout=None, allow_redirects=None):
        self.calls.append({"url": url, "headers": dict(headers or {})})
        return self.pages[min(len(self.calls) - 1, len(self.pages) - 1)]


def feed(records, has_next=False, etag="W/\"abc\""):
    return FakeResponse(200, {"records": records, "has_next_page": has_next}, etag=etag)


def state_for(db, community_id, **overrides):
    with db.session() as s:
        row = s.scalar(
            __import__("sqlalchemy").select(WatchState).where(
                WatchState.community_id == community_id)
        )
        base = {
            "id": row.id, "community_id": row.community_id, "host": row.host,
            "mode": row.mode, "tier": row.tier, "last_post_id": row.last_post_id,
            "recent_ids": list(row.recent_ids or []), "etag": row.etag,
            "consecutive_errors": row.consecutive_errors,
        }
    base.update(overrides)
    return base


def read_row(db, community_id) -> WatchState:
    from sqlalchemy import select
    with db.session() as s:
        row = s.scalar(select(WatchState).where(WatchState.community_id == community_id))
        s.expunge(row)
        return row


class Requirements:
    excluded_content: list = []
    max_post_age_days = 0


def triage_spy(calls, leads=None):
    """Stand in for triage_records, with the same shape the caller reads.

    ``new_leads`` is a property on the real TriageResult, and the poller uses
    it to decide what to report -- a spy without it hides that.
    """
    def _triage(db, records, requirements, **kw):
        calls.append({"records": records, "community": kw.get("community")})

        class R:
            def __init__(self):
                self.leads = list(leads or [])
                self.already_seen = 0

            @property
            def new_leads(self):
                return [x for x in self.leads if not x.get("is_duplicate")]
        return R()
    return _triage


# --- the watch list -------------------------------------------------------

def test_ensure_watch_rows_picks_watched_communities(db, community):
    assert ensure_watch_rows(db) == 1
    assert read_row(db, community).host == HOST
    # Running it again must not add the same community twice.
    assert ensure_watch_rows(db) == 0


def test_a_new_row_starts_on_the_tier_its_history_earns(db, community):
    """Everything on the fast tier is 86 requests a minute for 173 communities.

    The poller would not go faster; it would fall behind on all of them,
    including the ones that actually post.
    """
    from datetime import datetime, timedelta

    from circle_leads.storage.database import get_or_create_community, upsert_post
    from circle_leads.storage.models import WatchState
    from sqlalchemy import select

    with db.session() as s:
        quiet = get_or_create_community(s, slug="quiet", url="https://quiet.circle.so",
                                        platform="circle", watching=True)
        s.flush()
        quiet_id = quiet.id
        upsert_post(s, community_id=community, record={
            "source_content_id": "1", "content": "We are hiring",
            "published_at": datetime.utcnow() - timedelta(days=1)})

    ensure_watch_rows(db, quiet_days=14)

    with db.session() as s:
        tiers = {r.community_id: r.tier for r in s.scalars(select(WatchState))}
    assert tiers[community] == "fast"      # posted yesterday
    assert tiers[quiet_id] == "slow"       # never posted


def test_a_community_without_a_host_is_not_watched(db):
    with db.session() as s:
        s.add(Community(slug="nohost", url="https://discover.circle.so/products/x",
                        platform="circle", watching=True, host=None))
    assert ensure_watch_rows(db) == 0


# --- the first pass -------------------------------------------------------

def test_first_pass_only_sets_the_watermark(db, community, monkeypatch):
    """The archive came in through the harvest; re-triaging it would re-send leads."""
    ensure_watch_rows(db)
    calls = []
    monkeypatch.setattr("circle_leads.triage.pipeline.triage_records", triage_spy(calls))

    session = FakeSession([feed([make_record(30), make_record(29)])])
    out = check_community(db, state_for(db, community), Requirements(),
                          session=session, tuning=WatchTuning(), use_llm=False)

    assert out.status == "ok" and out.new_posts == 0
    assert calls == []                      # nothing was triaged
    row = read_row(db, community)
    assert row.last_post_id == 30
    assert set(row.recent_ids) == {29, 30}


# --- finding new posts ----------------------------------------------------

def test_the_first_pass_reads_one_page_only(db, community, monkeypatch):
    """Seeding wants one number, not the archive.

    Every record on page one counts as new when there is no watermark yet, so
    without a guard the loop pages to max_pages on every community -- five
    times the requests, on every community in the list, to learn where each
    feed currently is.
    """
    ensure_watch_rows(db)
    monkeypatch.setattr("circle_leads.triage.pipeline.triage_records", triage_spy([]))

    pages = [
        feed([make_record(30), make_record(29)], has_next=True),
        feed([make_record(28), make_record(27)], has_next=True),
    ]
    session = FakeSession(pages)
    check_community(db, state_for(db, community), Requirements(),
                    session=session, tuning=WatchTuning(per_page=2))

    assert len(session.calls) == 1
    assert read_row(db, community).last_post_id == 30


def test_a_new_post_is_triaged_once_and_moves_the_watermark(db, community, monkeypatch):
    ensure_watch_rows(db)
    calls = []
    monkeypatch.setattr("circle_leads.triage.pipeline.triage_records", triage_spy(calls))
    tuning = WatchTuning()

    check_community(db, state_for(db, community), Requirements(),
                    session=FakeSession([feed([make_record(30)])]), tuning=tuning)

    out = check_community(db, state_for(db, community), Requirements(),
                          session=FakeSession([feed([make_record(31), make_record(30)])]),
                          tuning=tuning)

    assert out.new_posts == 1
    assert len(calls) == 1
    assert [r["source_content_id"] for r in calls[0]["records"]] == ["31"]
    assert calls[0]["community"] == "example"       # the slug the harvest uses
    assert read_row(db, community).last_post_id == 31


def test_an_already_seen_post_is_not_triaged_again(db, community, monkeypatch):
    ensure_watch_rows(db)
    calls = []
    monkeypatch.setattr("circle_leads.triage.pipeline.triage_records", triage_spy(calls))
    tuning = WatchTuning()

    check_community(db, state_for(db, community), Requirements(),
                    session=FakeSession([feed([make_record(30)])]), tuning=tuning)
    check_community(db, state_for(db, community), Requirements(),
                    session=FakeSession([feed([make_record(31)])]), tuning=tuning)
    calls.clear()
    out = check_community(db, state_for(db, community), Requirements(),
                          session=FakeSession([feed([make_record(31)])]), tuning=tuning)

    assert out.new_posts == 0 and calls == []


def test_a_burst_is_paged_back_until_a_known_id(db, community, monkeypatch):
    ensure_watch_rows(db)
    calls = []
    monkeypatch.setattr("circle_leads.triage.pipeline.triage_records", triage_spy(calls))
    tuning = WatchTuning(per_page=2)

    check_community(db, state_for(db, community), Requirements(),
                    session=FakeSession([feed([make_record(10)])]), tuning=tuning)

    pages = [
        feed([make_record(14), make_record(13)], has_next=True),
        feed([make_record(12), make_record(11)], has_next=True),
        feed([make_record(10), make_record(9)], has_next=True),
    ]
    out = check_community(db, state_for(db, community), Requirements(),
                          session=FakeSession(pages), tuning=tuning)

    assert out.new_posts == 4
    ids = [r["source_content_id"] for r in calls[0]["records"]]
    assert ids == ["11", "12", "13", "14"]          # oldest first
    assert read_row(db, community).last_post_id == 14


def test_a_page_of_only_pinned_posts_does_not_hide_what_is_below(db, community, monkeypatch):
    """A pinned post sits at the top forever; stopping there loses every new post."""
    ensure_watch_rows(db)
    calls = []
    monkeypatch.setattr("circle_leads.triage.pipeline.triage_records", triage_spy(calls))
    tuning = WatchTuning(per_page=2)

    check_community(db, state_for(db, community), Requirements(),
                    session=FakeSession([feed([make_record(5, pinned=True)])]),
                    tuning=tuning)

    pages = [
        feed([make_record(5, pinned=True), make_record(4, pinned=True)], has_next=True),
        feed([make_record(7), make_record(3)], has_next=False),
    ]
    out = check_community(db, state_for(db, community), Requirements(),
                          session=FakeSession(pages), tuning=tuning)

    assert out.new_posts == 1
    assert [r["source_content_id"] for r in calls[0]["records"]] == ["7"]


# --- failures must not move the watermark ---------------------------------

@pytest.mark.parametrize("response,expected", [
    (FakeResponse(429), "ratelimited"),
    (FakeResponse(503, text="<title>just a moment</title>"), "challenge"),
    (FakeResponse(500), "http_500"),
])
def test_a_refused_read_keeps_the_watermark(db, community, monkeypatch, response, expected):
    ensure_watch_rows(db)
    monkeypatch.setattr("circle_leads.triage.pipeline.triage_records", triage_spy([]))
    tuning = WatchTuning()

    check_community(db, state_for(db, community), Requirements(),
                    session=FakeSession([feed([make_record(30)])]), tuning=tuning)
    before = read_row(db, community).last_post_id

    out = check_community(db, state_for(db, community), Requirements(),
                          session=FakeSession([response]), tuning=tuning)

    assert out.status == expected
    row = read_row(db, community)
    assert row.last_post_id == before
    assert row.consecutive_errors == 1


def test_a_network_error_keeps_the_watermark(db, community, monkeypatch):
    ensure_watch_rows(db)
    tuning = WatchTuning()
    monkeypatch.setattr("circle_leads.triage.pipeline.triage_records", triage_spy([]))
    check_community(db, state_for(db, community), Requirements(),
                    session=FakeSession([feed([make_record(30)])]), tuning=tuning)

    class Broken(FakeSession):
        def get(self, *a, **k):
            raise requests.ConnectTimeout("nope")

    out = check_community(db, state_for(db, community), Requirements(),
                          session=Broken([]), tuning=tuning)
    assert out.status == "error"
    assert read_row(db, community).last_post_id == 30


def test_repeated_failures_back_off(db, community, monkeypatch):
    ensure_watch_rows(db)
    monkeypatch.setattr("circle_leads.triage.pipeline.triage_records", triage_spy([]))
    tuning = WatchTuning()
    gaps = []
    for _ in range(3):
        check_community(db, state_for(db, community), Requirements(),
                        session=FakeSession([FakeResponse(429)]), tuning=tuning)
        row = read_row(db, community)
        gaps.append(row.next_check_at - row.last_checked_at)
    assert gaps[0] < gaps[1] < gaps[2]


# --- 304, the cheap path --------------------------------------------------

def test_not_modified_costs_nothing_and_keeps_state(db, community, monkeypatch):
    ensure_watch_rows(db)
    calls = []
    monkeypatch.setattr("circle_leads.triage.pipeline.triage_records", triage_spy(calls))
    tuning = WatchTuning()

    check_community(db, state_for(db, community), Requirements(),
                    session=FakeSession([feed([make_record(30)], etag='W/"v1"')]),
                    tuning=tuning)

    session = FakeSession([FakeResponse(304)])
    out = check_community(db, state_for(db, community), Requirements(),
                          session=session, tuning=tuning)

    assert out.status == "not_modified" and calls == []
    assert session.calls[0]["headers"].get("If-None-Match") == 'W/"v1"'
    assert read_row(db, community).last_post_id == 30


# --- access changes -------------------------------------------------------

def test_a_feed_that_closes_without_a_session_is_switched_off(db, community, monkeypatch):
    ensure_watch_rows(db)
    monkeypatch.setattr("circle_leads.triage.pipeline.triage_records", triage_spy([]))
    out = check_community(db, state_for(db, community), Requirements(),
                          session=FakeSession([FakeResponse(401)]), tuning=WatchTuning())
    assert out.status == "unauthorized"
    assert read_row(db, community).mode == WatchMode.OFF.value


def test_a_feed_that_closes_with_a_session_stored_switches_to_cookie(db, community, monkeypatch):
    from circle_leads.web.replay_store import connect_host
    ensure_watch_rows(db)
    connect_host(db, HOST, [{"name": "_circle_session", "value": "x"}],
                 member_label="test")
    monkeypatch.setattr("circle_leads.triage.pipeline.triage_records", triage_spy([]))

    check_community(db, state_for(db, community), Requirements(),
                    session=FakeSession([FakeResponse(401)]), tuning=WatchTuning())
    assert read_row(db, community).mode == WatchMode.COOKIE.value


def test_a_dead_host_is_switched_off(db, community, monkeypatch):
    ensure_watch_rows(db)
    monkeypatch.setattr("circle_leads.triage.pipeline.triage_records", triage_spy([]))
    check_community(db, state_for(db, community), Requirements(),
                    session=FakeSession([FakeResponse(404)]), tuning=WatchTuning())
    row = read_row(db, community)
    assert row.mode == WatchMode.OFF.value
    assert row.next_check_at - row.last_checked_at > timedelta(hours=12)


# --- tiers ----------------------------------------------------------------

def test_a_quiet_community_drops_to_the_slow_tier_and_a_post_brings_it_back(
    db, community, monkeypatch
):
    ensure_watch_rows(db)
    calls = []
    monkeypatch.setattr("circle_leads.triage.pipeline.triage_records", triage_spy(calls))
    tuning = WatchTuning(quiet_days=14)

    check_community(db, state_for(db, community), Requirements(),
                    session=FakeSession([feed([make_record(30)])]), tuning=tuning)

    from sqlalchemy import select
    with db.session() as s:
        row = s.scalar(select(WatchState).where(WatchState.community_id == community))
        row.created_at = datetime(2026, 1, 1)
        row.last_new_at = None

    check_community(db, state_for(db, community), Requirements(),
                    session=FakeSession([feed([make_record(30)])]), tuning=tuning)
    assert read_row(db, community).tier == "slow"

    check_community(db, state_for(db, community), Requirements(),
                    session=FakeSession([feed([make_record(31), make_record(30)])]),
                    tuning=tuning)
    assert read_row(db, community).tier == "fast"


def test_the_fast_tier_is_checked_sooner_than_the_slow_one(db, community, monkeypatch):
    ensure_watch_rows(db)
    monkeypatch.setattr("circle_leads.triage.pipeline.triage_records", triage_spy([]))
    tuning = WatchTuning(fast_interval=120, slow_interval=900, jitter=0.0)

    # Seed, then find a post: that promotion is what puts a community on the
    # fast tier in the first place.
    check_community(db, state_for(db, community), Requirements(),
                    session=FakeSession([feed([make_record(30)])]), tuning=tuning)
    check_community(db, state_for(db, community), Requirements(),
                    session=FakeSession([feed([make_record(31), make_record(30)])]),
                    tuning=tuning)
    assert read_row(db, community).tier == "fast"
    fast_gap = (read_row(db, community).next_check_at
                - read_row(db, community).last_checked_at)

    # The row's tier is what counts, not what the caller was handed: the poller
    # re-reads it so a community promoted mid-cycle is scheduled correctly.
    from sqlalchemy import select
    with db.session() as s:
        row = s.scalar(select(WatchState).where(WatchState.community_id == community))
        row.tier = "slow"
        row.last_new_at = datetime(2026, 1, 1)

    check_community(db, state_for(db, community), Requirements(),
                    session=FakeSession([FakeResponse(304)]), tuning=tuning)
    slow_gap = (read_row(db, community).next_check_at
                - read_row(db, community).last_checked_at)

    assert fast_gap < slow_gap


# --- reporting ------------------------------------------------------------

def test_a_new_lead_is_reported_once(db, community, monkeypatch):
    """The lead is already saved and already on its way to Vini by then."""
    ensure_watch_rows(db)
    sent = []
    monkeypatch.setattr("circle_leads.notify.send",
                        lambda text, **kw: sent.append((text, kw.get("dedup_key"))) or True)
    lead = {"job_title": "Flutter developer", "lead_score": 71,
            "budget": "8k", "url": "https://example.circle.so/c/jobs/post-31",
            "evidence_quote": "We need someone for three months",
            "is_duplicate": False}
    monkeypatch.setattr("circle_leads.triage.pipeline.triage_records",
                        triage_spy([], leads=[lead]))
    tuning = WatchTuning()

    from circle_leads.watch.poller import run_watch  # noqa: F401  (import path check)

    check_community(db, state_for(db, community), Requirements(),
                    session=FakeSession([feed([make_record(30)])]), tuning=tuning)
    out = check_community(db, state_for(db, community), Requirements(),
                          session=FakeSession([feed([make_record(31), make_record(30)])]),
                          tuning=tuning)

    assert len(out.lead_payloads) == 1
    from circle_leads.watch.poller import _report_leads
    _report_leads(out)
    assert len(sent) == 1
    text, key = sent[0]
    assert "Flutter developer" in text
    assert key == "lead:https://example.circle.so/c/jobs/post-31:example"


def test_a_duplicate_lead_is_not_reported(db, community, monkeypatch):
    ensure_watch_rows(db)
    lead = {"job_title": "Flutter developer", "is_duplicate": True}
    monkeypatch.setattr("circle_leads.triage.pipeline.triage_records",
                        triage_spy([], leads=[lead]))
    tuning = WatchTuning()

    check_community(db, state_for(db, community), Requirements(),
                    session=FakeSession([feed([make_record(30)])]), tuning=tuning)
    out = check_community(db, state_for(db, community), Requirements(),
                          session=FakeSession([feed([make_record(31), make_record(30)])]),
                          tuning=tuning)
    assert out.lead_payloads == []


def test_reporting_failure_does_not_break_the_poller(db, community, monkeypatch):
    ensure_watch_rows(db)
    lead = {"job_title": "Flutter developer", "is_duplicate": False}
    monkeypatch.setattr("circle_leads.triage.pipeline.triage_records",
                        triage_spy([], leads=[lead]))
    monkeypatch.setattr("circle_leads.notify.send",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("telegram down")))
    tuning = WatchTuning()

    check_community(db, state_for(db, community), Requirements(),
                    session=FakeSession([feed([make_record(30)])]), tuning=tuning)
    out = check_community(db, state_for(db, community), Requirements(),
                          session=FakeSession([feed([make_record(31), make_record(30)])]),
                          tuning=tuning)
    from circle_leads.watch.poller import _report_leads
    _report_leads(out)                       # must not raise
    assert read_row(db, community).last_post_id == 31


# --- who gets into the watch list at all ----------------------------------

def test_a_directory_community_is_still_a_circle_community(db):
    """platform="discover" meant "found via Circle's own directory", not "not Circle".

    Matching only platform="circle" here left eight ICP-fit communities out of
    the watch list, three of them with posts already read and a stored session.
    """
    with db.session() as s:
        s.add(Community(slug="viadirectory", url="https://viadirectory.circle.so",
                        platform="discover", host="viadirectory.circle.so",
                        icp_flag=True))
    assert ensure_watch_rows(db) == 1


def test_a_community_on_another_platform_is_not_watched(db):
    """A marketing site that merely had a Circle directory card has no feed."""
    with db.session() as s:
        s.add(Community(slug="notcircle", url="https://example.com",
                        platform="other", host="example.com", icp_flag=True))
    assert ensure_watch_rows(db) == 0


def test_the_running_poller_picks_up_communities_added_after_it_started(db, monkeypatch):
    """The list used to be built only by `watch --sync`, which the service never passes.

    Everything discovery and the ICP pass found after the last manual sync was
    invisible to the poller until someone restarted it with the flag.
    """
    from circle_leads.watch import poller

    with db.session() as s:
        s.add(Community(slug="late", url="https://late.circle.so",
                        platform="circle", host="late.circle.so", icp_flag=True))

    # No network: the loop should add the row before it looks for work.
    monkeypatch.setattr(poller, "due_states", lambda _db: [])
    poller.run_watch(db, once=True)

    from sqlalchemy import select

    with db.session() as s:
        hosts = set(s.scalars(select(WatchState.host)).all())
    assert "late.circle.so" in hosts
