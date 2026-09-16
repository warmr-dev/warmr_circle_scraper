"""Auto-join orchestration (circle_leads/join/): candidate gating, pacing, and
the ego-browser bridge -- all without a real browser. ``attempt_join``/
``open_join_space`` are monkeypatched at the module level the same way
tests/test_job_queue.py exercises the queue with a real (SQLite) Database but
no real network."""

from __future__ import annotations

import json
import subprocess
import tempfile
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from circle_leads.config.settings import JoinPacingConfig
from circle_leads.join import ego_bridge, joiner
from circle_leads.join.ego_bridge import EgoBrowserError, EgoJoinResult
from circle_leads.join.pacing import joins_attempted_today
from circle_leads.storage.database import Database, get_or_create_community
from circle_leads.storage.models import Community, JoinStatus


@pytest.fixture(autouse=True)
def _isolate_attempt_log(tmp_path, monkeypatch):
    # Without this every test run appends to the real data/join_attempts.log
    # -- which _attempts_today_for_account() reads to decide a live account's
    # remaining daily cap. A stray test-seeded "joined" row for a fake
    # account there silently eats into a real budget.
    monkeypatch.setattr(joiner, "ATTEMPT_LOG_PATH", tmp_path / "join_attempts.log")


def _db():
    return Database("sqlite:///" + tempfile.mktemp(suffix=".db"))


def _seed(db, slug, *, icp_flag=True, platform="circle", join_type="free_join",
          join_status=JoinStatus.NOT_ATTEMPTED.value, icp_score=0.0, url=None, name=None):
    with db.session() as s:
        c = get_or_create_community(s, slug=slug, url=url or f"https://{slug}.circle.so")
        if name is not None:
            c.name = name
        c.icp_flag = icp_flag
        c.platform = platform
        c.join_type = join_type
        c.join_status = join_status
        c.icp_score = icp_score


# --- select_join_candidates ---------------------------------------------------


def test_gates_on_icp_flag_platform_join_type_and_status():
    db = _db()
    _seed(db, "good", icp_score=10)
    _seed(db, "not-icp-fit", icp_flag=False, icp_score=99)
    _seed(db, "not-circle", platform="skool", icp_score=99)
    _seed(db, "invite-only", join_type="invite_only", icp_score=99)
    _seed(db, "already-joined", join_status=JoinStatus.JOINED.value, icp_score=99)

    candidates = joiner.select_join_candidates(db)
    assert [c["slug"] for c in candidates] == ["good"]


def test_orders_by_icp_score_descending_and_respects_limit():
    db = _db()
    _seed(db, "low", icp_score=10)
    _seed(db, "high", icp_score=50)
    _seed(db, "mid", icp_score=30)

    assert [c["slug"] for c in joiner.select_join_candidates(db)] == ["high", "mid", "low"]
    assert [c["slug"] for c in joiner.select_join_candidates(db, limit=1)] == ["high"]


def test_paid_join_type_is_included():
    db = _db()
    _seed(db, "paid-one", join_type="paid", icp_score=5)
    assert [c["slug"] for c in joiner.select_join_candidates(db)] == ["paid-one"]


# --- pacing --------------------------------------------------------------------


def test_joins_attempted_today_counts_only_todays_attempts():
    db = _db()
    with db.session() as s:
        old = get_or_create_community(s, slug="yesterday", url="https://yesterday.circle.so")
        old.join_attempted_at = datetime.utcnow() - timedelta(days=1)
        today = get_or_create_community(s, slug="today", url="https://today.circle.so")
        today.join_attempted_at = datetime.utcnow()
        get_or_create_community(s, slug="never-attempted", url="https://never.circle.so")

    assert joins_attempted_today(db) == 1


# --- run_auto_join: batch orchestration -----------------------------------------


def test_dry_run_lists_candidates_without_touching_the_bridge(monkeypatch):
    db = _db()
    _seed(db, "candidate", icp_score=10)

    def boom(*a, **kw):
        raise AssertionError("dry-run must not call the ego-browser bridge")

    monkeypatch.setattr(joiner, "open_join_space", boom)
    monkeypatch.setattr(joiner, "attempt_join", boom)

    result = joiner.run_auto_join(db, dry_run=True)
    assert result.attempted == ["candidate"]
    assert result.joined == []


def test_short_slug_host_filter_does_not_substring_match_other_urls(monkeypatch):
    # Regression: a short slug like "s" used to substring-match almost every
    # candidate's URL (they all contain "https"), silently sweeping unrelated
    # communities into a single --host invocation.
    db = _db()
    _seed(db, "s", url="https://s.circle.so", icp_score=10)
    _seed(db, "other-community", url="https://other-community.circle.so", icp_score=99)

    def boom(*a, **kw):
        raise AssertionError("must not touch the bridge in a dry run")

    monkeypatch.setattr(joiner, "open_join_space", boom)
    monkeypatch.setattr(joiner, "attempt_join", boom)

    result = joiner.run_auto_join(db, host="s", dry_run=True)
    assert result.attempted == ["s"]


def test_joined_outcome_is_persisted_and_batch_continues(monkeypatch):
    db = _db()
    _seed(db, "a", icp_score=20)
    _seed(db, "b", icp_score=10)

    monkeypatch.setattr(joiner, "open_join_space", lambda name: 42)
    monkeypatch.setattr(
        joiner, "attempt_join",
        lambda space_id, url, **kw: EgoJoinResult(status="joined", detail="ok"),
    )
    monkeypatch.setattr(joiner, "sleep_between_attempts", lambda pacing: None)

    result = joiner.run_auto_join(db, pacing=JoinPacingConfig(max_joins_per_day=25))

    assert result.space_id == 42
    assert result.attempted == ["a", "b"]
    assert result.joined == ["a", "b"]
    assert result.stopped_for is None

    with db.session() as s:
        row = s.scalar(select(Community).where(Community.slug == "a"))
        assert row.join_status == JoinStatus.JOINED.value
        assert row.joined_at is not None
        assert row.join_attempts == 1


def test_joined_with_cookies_wires_the_host_into_replay_store(monkeypatch):
    # The entire join -> scrape hookup: without this, a real join never
    # reaches scan_cookie_host()/cookie_hosts_vip_first() (circle_leads/
    # scanning.py), which is exactly the gap that left 24 real joins from
    # this project's own sessions producing zero scraped leads.
    db = _db()
    _seed(db, "a", icp_score=20, url="https://discover.circle.so/products/a", name="A Community")

    cookies = [{"name": "_circle_session", "value": "s3cr3t", "domain": "a-real-host.circle.so"}]
    monkeypatch.setattr(joiner, "open_join_space", lambda name: 1)
    monkeypatch.setattr(
        joiner, "attempt_join",
        lambda space_id, url, **kw: EgoJoinResult(status="joined", detail="ok", cookies=cookies),
    )
    monkeypatch.setattr(joiner, "sleep_between_attempts", lambda pacing: None)

    seen = {}
    monkeypatch.setattr(
        joiner, "connect_host",
        lambda db_, host, cks, **kw: seen.update(host=host, cookies=cks, kw=kw),
    )

    joiner.run_auto_join(db)

    # The candidate's stored url is a discover.circle.so listing page -- the
    # cookie's own domain (the page's actual final host after any redirect)
    # is what gets connected, not that listing URL's host.
    assert seen["host"] == "a-real-host.circle.so"
    assert seen["cookies"] == cookies
    assert seen["kw"]["member_label"] == "A Community"


def test_joined_without_cookies_does_not_crash_the_batch(monkeypatch):
    db = _db()
    _seed(db, "a", icp_score=20)

    monkeypatch.setattr(joiner, "open_join_space", lambda name: 1)
    monkeypatch.setattr(
        joiner, "attempt_join",
        lambda space_id, url, **kw: EgoJoinResult(status="joined", detail="ok"),
    )
    monkeypatch.setattr(joiner, "sleep_between_attempts", lambda pacing: None)

    def boom(*a, **kw):
        raise AssertionError("must not call connect_host with no cookies")

    monkeypatch.setattr(joiner, "connect_host", boom)

    result = joiner.run_auto_join(db)
    assert result.joined == ["a"]


def test_credentials_are_read_from_env_and_passed_through(monkeypatch):
    """joiner reads CIRCLE_EMAIL/CIRCLE_PASSWORD itself (the account the user
    chose to reuse for auto-join) and forwards them to attempt_join, which is
    the only thing that embeds them into the ego-browser script."""
    db = _db()
    _seed(db, "a", icp_score=10)
    monkeypatch.setenv("CIRCLE_EMAIL", "z@example.com")
    monkeypatch.setenv("CIRCLE_PASSWORD", "s3cret")
    monkeypatch.setattr(joiner, "open_join_space", lambda name: 1)
    monkeypatch.setattr(joiner, "sleep_between_attempts", lambda pacing: None)

    seen_kwargs = {}

    def fake_attempt_join(space_id, url, **kw):
        seen_kwargs.update(kw)
        return EgoJoinResult(status="joined", detail="ok")

    monkeypatch.setattr(joiner, "attempt_join", fake_attempt_join)

    joiner.run_auto_join(db)

    assert seen_kwargs["email"] == "z@example.com"
    assert seen_kwargs["password"] == "s3cret"


def test_account_test_reads_the_second_credential_pair(monkeypatch):
    db = _db()
    _seed(db, "a", icp_score=10)
    monkeypatch.setenv("CIRCLE_EMAIL2", "test-acct@example.com")
    monkeypatch.setenv("CIRCLE_PASSWORD2", "t3st")
    monkeypatch.setattr(joiner, "open_join_space", lambda name: 1)
    monkeypatch.setattr(joiner, "sleep_between_attempts", lambda pacing: None)

    seen_kwargs = {}

    def fake_attempt_join(space_id, url, **kw):
        seen_kwargs.update(kw)
        return EgoJoinResult(status="joined", detail="ok")

    monkeypatch.setattr(joiner, "attempt_join", fake_attempt_join)

    joiner.run_auto_join(db, account="test")

    assert seen_kwargs["email"] == "test-acct@example.com"
    assert seen_kwargs["password"] == "t3st"


def test_test_account_daily_cap_is_independent_of_main(monkeypatch, tmp_path):
    # The DB's join_attempted_at has no per-account marker -- it's whatever
    # "main" has always used. If a fresh "test" account's cap check ever fell
    # back to that same DB-wide count, main having already used its 25 today
    # would incorrectly block test's very first attempt.
    db = _db()
    # icp_flag=False keeps it out of select_join_candidates -- it's here only
    # to inflate the DB-wide join_attempted_at count that joins_attempted_today
    # (main's path) would see, already join_status JOINED so it doesn't need
    # icp_flag to have been real to begin with.
    _seed(db, "already-attempted-by-main", icp_flag=False, join_status=JoinStatus.JOINED.value)
    with db.session() as s:
        row = s.scalar(select(Community).where(Community.slug == "already-attempted-by-main"))
        row.join_attempted_at = datetime.utcnow()  # main used its slot today

    _seed(db, "candidate", icp_score=10)
    monkeypatch.setattr(joiner, "ATTEMPT_LOG_PATH", tmp_path / "join_attempts.log")
    monkeypatch.setenv("CIRCLE_EMAIL2", "test-acct@example.com")
    monkeypatch.setenv("CIRCLE_PASSWORD2", "t3st")
    monkeypatch.setattr(joiner, "open_join_space", lambda name: 1)
    monkeypatch.setattr(joiner, "attempt_join", lambda *a, **kw: EgoJoinResult(status="joined", detail="ok"))
    monkeypatch.setattr(joiner, "sleep_between_attempts", lambda pacing: None)

    result = joiner.run_auto_join(db, account="test", pacing=JoinPacingConfig(max_joins_per_day=1))

    assert result.joined == ["candidate"]


def test_handoff_status_stops_the_batch_without_persisting(monkeypatch):
    db = _db()
    _seed(db, "a", icp_score=20)
    _seed(db, "b", icp_score=10)

    monkeypatch.setattr(joiner, "open_join_space", lambda name: 7)
    monkeypatch.setattr(
        joiner, "attempt_join",
        lambda space_id, url, **kw: EgoJoinResult(status="needs_login", detail="log in please"),
    )
    monkeypatch.setattr(joiner, "sleep_between_attempts", lambda pacing: None)

    result = joiner.run_auto_join(db)

    assert result.attempted == ["a"]
    assert result.joined == []
    assert result.stopped_for == "a"
    assert "needs_login" in result.stop_reason

    with db.session() as s:
        row = s.scalar(select(Community).where(Community.slug == "a"))
        assert row.join_status == JoinStatus.NOT_ATTEMPTED.value  # left untouched, not "failed"


def test_handoff_outcome_is_logged_even_though_not_persisted_to_the_db(monkeypatch, tmp_path):
    # application_form_detected (and every other handoff) used to vanish
    # entirely once the process exited -- nothing in the DB (by design, see
    # the test above) and nothing durable anywhere else. This is the only
    # record that a given UI/form variant was ever seen.
    db = _db()
    _seed(db, "a", icp_score=20)

    log_path = tmp_path / "join_attempts.log"
    monkeypatch.setattr(joiner, "ATTEMPT_LOG_PATH", log_path)
    monkeypatch.setattr(joiner, "open_join_space", lambda name: 7)
    monkeypatch.setattr(
        joiner, "attempt_join",
        lambda space_id, url, **kw: EgoJoinResult(
            status="application_form_detected", detail='Questions: ["Why do you want to join?"]'
        ),
    )
    monkeypatch.setattr(joiner, "sleep_between_attempts", lambda pacing: None)

    joiner.run_auto_join(db)

    lines = log_path.read_text().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["slug"] == "a"
    assert entry["status"] == "application_form_detected"
    assert "Why do you want to join?" in entry["detail"]


def test_daily_cap_blocks_the_batch_before_any_bridge_call(monkeypatch):
    db = _db()
    _seed(db, "a", icp_score=10)
    with db.session() as s:
        row = s.scalar(select(Community).where(Community.slug == "a"))
        row.join_attempted_at = datetime.utcnow()  # counts toward today's cap

    def boom(*a, **kw):
        raise AssertionError("must not touch the bridge once the daily cap is reached")

    monkeypatch.setattr(joiner, "open_join_space", boom)
    monkeypatch.setattr(joiner, "attempt_join", boom)

    with pytest.raises(RuntimeError, match="cap"):
        joiner.run_auto_join(db, pacing=JoinPacingConfig(max_joins_per_day=1))


def test_bridge_error_stops_the_batch_and_is_not_recorded_as_failed(monkeypatch):
    db = _db()
    _seed(db, "a", icp_score=10)

    monkeypatch.setattr(joiner, "open_join_space", lambda name: 1)

    def raise_bridge_error(space_id, url, **kw):
        raise EgoBrowserError("ego-browser not installed")

    monkeypatch.setattr(joiner, "attempt_join", raise_bridge_error)

    result = joiner.run_auto_join(db)
    assert result.attempted == []
    assert result.stopped_for == "a"
    assert "ego-browser not installed" in result.stop_reason

    with db.session() as s:
        row = s.scalar(select(Community).where(Community.slug == "a"))
        assert row.join_status == JoinStatus.NOT_ATTEMPTED.value


# --- ego_bridge: subprocess plumbing, no real ego-browser ----------------------


class _FakeCompleted:
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def test_open_join_space_parses_the_id_line(monkeypatch):
    monkeypatch.setattr(
        ego_bridge.subprocess, "run",
        lambda *a, **kw: _FakeCompleted(stdout="EGO_JOIN_SPACE_ID=99\n"),
    )
    assert ego_bridge.open_join_space("test space") == 99


def test_attempt_join_parses_the_result_line(monkeypatch):
    monkeypatch.setattr(
        ego_bridge.subprocess, "run",
        lambda *a, **kw: _FakeCompleted(
            stdout='some log line\nEGO_JOIN_RESULT={"status": "joined", "detail": "ok", "screenshot": null}\n'
        ),
    )
    result = ego_bridge.attempt_join(1, "https://x.circle.so")
    assert result.status == "joined"
    assert result.detail == "ok"


def test_attempt_join_reads_the_result_from_stderr(monkeypatch):
    """ego-browser nodejs writes console.log() to stderr, not stdout --
    confirmed live, the reverse of the usual convention. Regression guard for
    that surprise: the earlier stdout-only version silently raised
    EgoBrowserError("no EGO_JOIN_RESULT line") on every real run."""
    monkeypatch.setattr(
        ego_bridge.subprocess, "run",
        lambda *a, **kw: _FakeCompleted(
            stdout="", stderr='EGO_JOIN_RESULT={"status": "needs_login", "detail": "log in", "screenshot": null}\n'
        ),
    )
    result = ego_bridge.attempt_join(1, "https://x.circle.so")
    assert result.status == "needs_login"


def test_open_join_space_reads_the_id_from_stderr(monkeypatch):
    monkeypatch.setattr(
        ego_bridge.subprocess, "run",
        lambda *a, **kw: _FakeCompleted(stdout="", stderr="EGO_JOIN_SPACE_ID=5\n"),
    )
    assert ego_bridge.open_join_space("test space") == 5


def test_missing_binary_raises_ego_browser_error(monkeypatch):
    def raise_not_found(*a, **kw):
        raise FileNotFoundError()

    monkeypatch.setattr(ego_bridge.subprocess, "run", raise_not_found)
    with pytest.raises(EgoBrowserError, match="not found"):
        ego_bridge.open_join_space("x")


def test_nonzero_exit_raises_ego_browser_error(monkeypatch):
    monkeypatch.setattr(
        ego_bridge.subprocess, "run",
        lambda *a, **kw: _FakeCompleted(stderr="boom", returncode=1),
    )
    with pytest.raises(EgoBrowserError, match="boom"):
        ego_bridge.open_join_space("x")


def test_missing_result_line_raises_ego_browser_error(monkeypatch):
    monkeypatch.setattr(
        ego_bridge.subprocess, "run",
        lambda *a, **kw: _FakeCompleted(stdout="nothing useful\n"),
    )
    with pytest.raises(EgoBrowserError, match="no EGO_JOIN_RESULT"):
        ego_bridge.attempt_join(1, "https://x.circle.so")


def test_timeout_raises_ego_browser_error(monkeypatch):
    def raise_timeout(*a, **kw):
        raise subprocess.TimeoutExpired(cmd="ego-browser", timeout=5)

    monkeypatch.setattr(ego_bridge.subprocess, "run", raise_timeout)
    with pytest.raises(EgoBrowserError, match="timed out"):
        ego_bridge.open_join_space("x")
