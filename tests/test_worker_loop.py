"""The CLI surface that runs unattended: the harvest command's worker modes,
the durable queue's job kinds, the scheduled sweep, and the few flags whose only
caller is the automation.

The worker's loop is infinite by design, so these run it for real and stop it
from its idle sleep -- the one point outside every try block (see _StopWorker).
A job kind that is never dispatched is a stage that only ever happens when
someone types the command, which is how 99.6% of the database got there.
"""

from __future__ import annotations

import tempfile
import time

from click.testing import CliRunner

from circle_leads.cli.main import cli
from circle_leads.storage.database import Database
from circle_leads.storage.settings_store import set_schedule


def _db_url():
    url = "sqlite:///" + tempfile.mktemp(suffix=".db")
    set_schedule(Database(url), "off")  # off -> never due
    return url


def test_scheduled_skips_when_not_due():
    url = _db_url()
    res = CliRunner().invoke(cli, ["--db", url, "harvest", "--scheduled"])
    assert res.exit_code == 0
    assert "not due yet" in res.output


def test_loop_flag_is_available():
    # --db explicit even for --help: the group callback runs (and builds a
    # Database) before click renders subcommand help, so an omitted --db would
    # fall through to CIRCLE_LEADS_DB (cli/main.py's new prod-default fallback)
    # if it happens to be set in the environment/.env -- a real network call
    # to prod from a test run, not just a local sqlite temp file.
    res = CliRunner().invoke(cli, ["--db", _db_url(), "harvest", "--help"])
    assert res.exit_code == 0
    assert "--loop" in res.output
    assert "--poll-seconds" in res.output


# --- The durable queue's job kinds -------------------------------------------
#
# The worker is the only thing that runs unattended, so a stage it cannot
# dispatch is a stage that only happens when a human types the command. These
# cover the two that were unreachable (directory discovery, enrichment) and the
# scheduled sweep that decides whether the LLM ever sees a community at all.


class _StopWorker(SystemExit):
    """Ends the worker's `while True` from its sleep, which is the only point
    outside every try block -- an ordinary exception raised inside the loop
    would be caught and recorded as a job error instead of stopping the run."""


def _worker_db(monkeypatch):
    """A worker DB with every schedule off, so only the queue drives the run."""
    from circle_leads.storage.settings_store import set_icp_schedule

    url = _db_url()
    set_icp_schedule(Database(url), "off")
    # The worker treats a key in the environment as "LLM available"; tests that
    # care about that decide it explicitly rather than inheriting the operator's.
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    return url


def _run_worker(url, monkeypatch, *, extra_args=()):
    """Run the worker until its first idle sleep, then stop it."""
    monkeypatch.setattr(time, "sleep", lambda *_a, **_kw: (_ for _ in ()).throw(_StopWorker()))
    return CliRunner().invoke(cli, ["--db", url, "worker", *extra_args])


def _job_rows(url):
    from circle_leads.storage.job_queue import recent

    return {j["kind"] + (f":{j['host']}" if j["host"] else ""): j
            for j in recent(Database(url))}


def test_enrich_job_kind_runs_the_enrichment_stage(monkeypatch):
    from circle_leads.cli import main as cli_main
    from circle_leads.storage.job_queue import enqueue

    url = _worker_db(monkeypatch)
    enqueue(Database(url), "enrich")
    calls = []
    monkeypatch.setattr(cli_main, "enrich_pending", lambda db, **kw: (
        calls.append(kw) or {"visited": 7, "named": 5, "described": 3,
                             "boilerplate_rejected": 1, "nothing_found": 2,
                             "skipped": 0, "wrapped": 0, "cursor": 42}
    ))

    _run_worker(url, monkeypatch)

    assert len(calls) == 1
    job = _job_rows(url)["enrich"]
    assert job["state"] == "done"
    assert job["result"]["visited"] == 7
    assert "visited 7" in job["detail"]


def test_discover_directory_job_kind_crawls_incrementally(monkeypatch):
    from types import SimpleNamespace

    from circle_leads.discovery import circle_directory
    from circle_leads.storage.job_queue import enqueue

    url = _worker_db(monkeypatch)
    enqueue(Database(url), "discover_directory")
    crawl_kwargs = {}

    def fake_crawl(**kw):
        crawl_kwargs.update(kw)
        return SimpleNamespace(
            listings=[SimpleNamespace(external_id="dir-1")], goals=["g"], errors=[]
        )

    monkeypatch.setattr(circle_directory, "crawl_directory", fake_crawl)
    monkeypatch.setattr(
        circle_directory, "persist_crawl_result",
        lambda db, result, min_score=0: SimpleNamespace(
            new_count=1, updated=[], unchanged=0
        ),
    )

    _run_worker(url, monkeypatch)

    assert crawl_kwargs == {"resolve_join_urls": True}
    job = _job_rows(url)["discover_directory"]
    assert job["state"] == "done"
    assert job["result"]["new"] == 1


def test_a_failing_new_job_kind_is_recorded_and_the_loop_continues(monkeypatch):
    from circle_leads.cli import main as cli_main
    from circle_leads.storage.job_queue import enqueue

    url = _worker_db(monkeypatch)
    db = Database(url)
    enqueue(db, "enrich")                       # this one raises
    enqueue(db, "enrich", host="second-batch")  # this one must still run
    attempts = []

    def flaky(db, **kw):
        attempts.append(kw)
        if len(attempts) == 1:
            raise RuntimeError("circle.so said no")
        return {"visited": 1, "named": 1, "described": 0, "boilerplate_rejected": 0,
                "nothing_found": 0, "skipped": 0, "wrapped": 0, "cursor": 1}

    monkeypatch.setattr(cli_main, "enrich_pending", flaky)

    _run_worker(url, monkeypatch)

    rows = _job_rows(url)
    assert rows["enrich"]["state"] == "error"
    assert "circle.so said no" in rows["enrich"]["detail"]
    # Same shape as the pre-existing kinds: the error lands on the job row and
    # the next job is claimed anyway.
    assert rows["enrich:second-batch"]["state"] == "done"
    assert len(attempts) == 2


def test_an_unknown_job_kind_still_completes(monkeypatch):
    from circle_leads.storage.job_queue import enqueue

    url = _worker_db(monkeypatch)
    enqueue(Database(url), "not-a-real-kind")

    _run_worker(url, monkeypatch)

    assert "unknown job kind" in _job_rows(url)["not-a-real-kind"]["detail"]


# --- The scheduled sweep -----------------------------------------------------


def _patch_scheduled_stages(monkeypatch):
    """Record the scheduled enrichment + ICP calls instead of running them."""
    from circle_leads.cli import main as cli_main

    order, icp_kwargs = [], []
    monkeypatch.setattr(cli_main, "enrich_pending", lambda db, **kw: (
        order.append("enrich") or {"visited": 0, "named": 0, "described": 0,
                                   "boilerplate_rejected": 0, "nothing_found": 0,
                                   "skipped": 0, "wrapped": 0, "cursor": 0}
    ))
    monkeypatch.setattr(cli_main, "classify_icp_pending", lambda db, req, **kw: (
        order.append("icp") or icp_kwargs.append(kw)
        or {"checked": 0, "flagged": 0, "not_flagged": 0}
    ))
    return order, icp_kwargs


def test_scheduled_sweep_escalates_to_the_llm(monkeypatch):
    """use_llm was never passed here, which is why icp_decided_by was 'rules'
    for all 12,476 production rows: the escalation had literally never run."""
    url = _db_url()  # ICP schedule left at its default -> due immediately
    monkeypatch.setenv("OPENAI_API_KEY", "test-key-not-real")
    order, icp_kwargs = _patch_scheduled_stages(monkeypatch)

    _run_worker(url, monkeypatch)

    assert icp_kwargs == [{"use_llm": True, "rescore_llm_eligible": True}]
    # Enrichment first: a community can only be judged on the text it has, and
    # classifying a blank row just files it as not-ICP with no evidence.
    assert order == ["enrich", "icp"]


def test_scheduled_sweep_runs_rules_only_without_a_key(monkeypatch):
    url = _db_url()
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    _order, icp_kwargs = _patch_scheduled_stages(monkeypatch)

    _run_worker(url, monkeypatch)

    # classify_icp_pending degrades to rules-only on its own, so this is about
    # the worker not claiming an LLM it hasn't got.
    assert icp_kwargs == [{"use_llm": False, "rescore_llm_eligible": True}]


def test_use_llm_flag_works_without_the_env_var(monkeypatch):
    url = _db_url()
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    _order, icp_kwargs = _patch_scheduled_stages(monkeypatch)

    _run_worker(url, monkeypatch, extra_args=("--use-llm",))

    assert icp_kwargs == [{"use_llm": True, "rescore_llm_eligible": True}]


# --- CLI wiring the automation depends on ------------------------------------


def test_discover_accepts_a_bare_custom_domain_only_when_asked():
    url = _db_url()
    args = ["--db", url, "discover", "--url", "community.acme.io", "--no-validate"]

    # Default stays closed: the gate exists for bulk web-search results.
    assert CliRunner().invoke(cli, args).exit_code == 2

    res = CliRunner().invoke(cli, [*args, "--allow-custom-domains"])
    assert res.exit_code == 0, res.output
    assert "Recorded 1" in res.output
    with Database(url).session() as s:
        from circle_leads.storage.models import Community

        assert s.query(Community).count() == 1


def test_auto_join_prints_the_hosts_waiting_on_a_human(monkeypatch):
    from circle_leads.join import joiner

    monkeypatch.setattr(
        joiner, "run_auto_join",
        lambda db, **kw: joiner.JoinBatchResult(
            space_id=7, attempted=["a", "b"], joined=["b"],
            handoffs={"a": "needs_login: unrecognized login form"},
        ),
    )

    res = CliRunner().invoke(cli, ["--db", _db_url(), "auto-join", "--limit", "2"])

    # A handoff no longer stops the batch, so it is only visible if printed.
    assert res.exit_code == 0, res.output
    assert "1 need a human" in res.output
    assert "needs_login" in res.output
    assert "--space-id 7" in res.output
