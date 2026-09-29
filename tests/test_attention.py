"""Section 5: the rules of "needs attention", and marking an item as seen.

Every rule must fire on its case and go quiet once the case is gone -- a
warning that cannot clear teaches the reader to ignore the colour. A mark
holds while the item stays as it was seen, and lets go when it changes.
"""

import json
from datetime import timedelta

import pytest

from circle_leads.storage.models import JOIN_HANDOFF_PREFIX, ActivityLog, JoinFormFill, ScanJob
from tests.dash_fixtures import (
    NOW, community, connection, lead, make_app, session_for, setting, watch,
)


def _rules(client):
    r = client.get("/api/dash/attention")
    assert r.status_code == 200, r.text
    return {rule["key"]: rule for rule in r.json()["rules"]}


def _keys(client, rule):
    return {item["key"] for item in _rules(client)[rule]["items"]}


def _fresh_beats(s):
    stamp = (NOW - timedelta(minutes=1)).isoformat()
    setting(s, "worker_heartbeat", stamp)
    setting(s, "watcher_heartbeat", stamp)


@pytest.fixture
def app(tmp_path, monkeypatch):
    client, db, app_ = make_app(tmp_path, monkeypatch)
    with db.session() as s:
        _fresh_beats(s)
    return client, db, app_


def _clear_cache(app_):
    app_.state.cache.clear()


def test_a_quiet_database_needs_nothing(app):
    client, _db, _app = app
    data = client.get("/api/dash/attention").json()
    assert data["open"] == 0
    assert all(rule["error"] is None for rule in data["rules"])


def test_a_silent_service(app):
    client, db, app_ = app
    with db.session() as s:
        setting(s, "worker_heartbeat", (NOW - timedelta(hours=2)).isoformat())
    _clear_cache(app_)
    assert _keys(client, "heartbeat") == {"worker"}
    with db.session() as s:
        _fresh_beats(s)
    _clear_cache(app_)
    assert _keys(client, "heartbeat") == set()


def test_a_failed_stage_until_it_works_again(app):
    client, db, app_ = app
    with db.session() as s:
        setting(s, "icp_classification_last_error",
                json.dumps({"at": (NOW - timedelta(hours=1)).isoformat(), "error": "icp: 402"}))
        setting(s, "icp_classification_last_finish", (NOW - timedelta(hours=3)).isoformat())
    _clear_cache(app_)
    assert _keys(client, "stage_error") == {"icp_classification"}
    with db.session() as s:
        setting(s, "icp_classification_last_finish", NOW.isoformat())
    _clear_cache(app_)
    assert _keys(client, "stage_error") == set()


def test_an_interrupted_harvest_but_not_a_running_one(app):
    client, db, app_ = app
    with db.session() as s:
        setting(s, "harvest_last_run", (NOW - timedelta(hours=1)).isoformat())
        setting(s, "harvest_last_finish", (NOW - timedelta(hours=7)).isoformat())
        setting(s, "worker_runtime", json.dumps({
            "beat_at": NOW.isoformat(), "stage": {"stage": "harvest"}, "env": {}, "llm": {"backend": "X"}}))
    _clear_cache(app_)
    assert _keys(client, "harvest_interrupted") == set()   # it is running
    with db.session() as s:
        setting(s, "worker_runtime", json.dumps({
            "beat_at": NOW.isoformat(), "stage": {"stage": None}, "env": {}, "llm": {"backend": "X"}}))
    _clear_cache(app_)
    assert _keys(client, "harvest_interrupted") == {"harvest"}


def test_missing_keys_in_a_service(app):
    client, db, app_ = app
    with db.session() as s:
        setting(s, "watcher_runtime", json.dumps({
            "beat_at": NOW.isoformat(), "stage": {}, "llm": {"backend": None},
            "env": {"VINI_API_SECRET": False, "SUPABASE_ANON_KEY": True}}))
    _clear_cache(app_)
    assert _keys(client, "runtime_env") == {"watcher:vini", "watcher:llm"}


def test_a_dead_session_but_not_a_cloudflare_block_or_a_site_that_left_circle(app):
    client, db, app_ = app
    with db.session() as s:
        connection(s, "dead.circle.so", "session_expired", "Session cookie invalid")
        connection(s, "cf.circle.so", "error", "Unexpected Cloudflare challenge on /internal_api")
        connection(s, "paused.circle.so", "session_expired", priority="paused")
        connection(s, "fine.circle.so", "connected")
        community(s, "moved", url="https://moved.example.com", platform="other")
        connection(s, "moved.example.com", "session_expired")
    _clear_cache(app_)
    assert _keys(client, "session_dead") == {"conn:dead.circle.so"}
    assert _keys(client, "session_cloudflare") == {"conn:cf.circle.so"}


def test_the_watcher_failing_and_a_member_community_switched_off(app):
    client, db, app_ = app
    with db.session() as s:
        watch(s, community(s, "failing"), last_status="error", consecutive_errors=7)
        watch(s, community(s, "throttled"), last_status="ratelimited", consecutive_errors=9)
        member = community(s, "member", join_status="joined")
        watch(s, member, mode="off", last_status="unauthorized")
        watch(s, community(s, "stranger"), mode="off", last_status="unauthorized")
    _clear_cache(app_)
    rules = _rules(client)
    assert {i["title"].split(":")[0] for i in rules["watch_failing"]["items"]} == {"failing.circle.so"}
    assert {i["title"].split(":")[0] for i in rules["watch_member_off"]["items"]} == {"member.circle.so"}


def test_joins_that_need_a_person(app):
    client, db, app_ = app
    with db.session() as s:
        community(s, "icecampus", join_status="needs_human", join_status_detail="passport")
        community(s, "profile", join_status="profile_pending")
        community(s, "stuck", join_status="not_attempted",
                  join_status_detail=f"{JOIN_HANDOFF_PREFIX} unclear — odd page (2026-09-29 10:00 UTC)")
        community(s, "cloudy", join_status="not_attempted",
                  join_status_detail=f"{JOIN_HANDOFF_PREFIX} challenge_stop — Cloudflare")
        community(s, "waiting", join_status="pending_approval",
                  join_attempted_at=NOW - timedelta(days=9))
        community(s, "fresh-wait", join_status="pending_approval",
                  join_attempted_at=NOW - timedelta(days=2))
    _clear_cache(app_)
    rules = _rules(client)
    assert len(rules["join_needs_human"]["items"]) == 2
    handoffs = {i["title"] for i in rules["join_handoff"]["items"]}
    assert any("причина неизвестна" in t for t in handoffs)
    assert any("challenge_stop" in t for t in handoffs)
    assert len(rules["join_pending_long"]["items"]) == 1


def test_a_form_question_until_someone_answers_it(app):
    client, db, app_ = app
    with db.session() as s:
        cid = community(s, "formy").id
        s.add(JoinFormFill(community_id=cid, host="formy.circle.so", account="test",
                           question_id=None, label="Passport number", outcome="needs_human",
                           created_at=NOW - timedelta(days=1)))
    _clear_cache(app_)
    assert _keys(client, "form_needs_human") == {"form:formy.circle.so"}
    with db.session() as s:
        s.add(JoinFormFill(community_id=cid, host="formy.circle.so", account="test",
                           question_id=None, label="Passport number", outcome="answered",
                           created_at=NOW))
    _clear_cache(app_)
    assert _keys(client, "form_needs_human") == set()


def test_leads_that_did_not_reach_the_client(app):
    client, db, app_ = app
    with db.session() as s:
        c = community(s, "acme")
        lead(s, c, created_at=NOW - timedelta(hours=3), author=False)                  # no author
        lead(s, c, created_at=NOW - timedelta(hours=3),
             reason="x. Held for review, not sent to Vini: no LLM verdict")            # held
        lead(s, c, created_at=NOW - timedelta(minutes=10))                             # too new
        lead(s, c, vini_status="held", vini_reason="invalid_timestamp missing_x")
        lead(s, c, vini_status="held", vini_reason="invalid_timestamp missing_x")
        lead(s, c, vini_status="accepted", external_synced_at=NOW)
    _clear_cache(app_)
    rules = _rules(client)
    assert _keys(client, "lead_unsent") == {"unsent:no_author", "unsent:held_for_review"}
    vini = rules["vini_not_accepted"]["items"]
    assert len(vini) == 1 and vini[0]["title"].startswith("2 ")


def test_jobs_queues_and_the_log(app):
    client, db, app_ = app
    with db.session() as s:
        s.add(ScanJob(kind="scan", host="x.circle.so", priority=1, state="error",
                      detail="RuntimeError: boom", created_at=NOW - timedelta(hours=1),
                      finished_at=NOW - timedelta(hours=1), attempts=1))
        s.add(ScanJob(kind="harvest", priority=1, state="queued",
                      created_at=NOW - timedelta(hours=2), attempts=0))
        for n in range(3):
            s.add(ActivityLog(kind="ingest", level="warning", community=f"c{n}",
                              summary=f"HTTP scan of host{n}.example.com: 0 post(s) — Session cookie invalid",
                              created_at=NOW - timedelta(hours=1), detail={},
                              items_seen=0, leads_found=0))
    _clear_cache(app_)
    rules = _rules(client)
    assert {i["key"] for i in rules["scan_jobs"]["items"]} >= {"queue"}
    assert len(rules["scan_jobs"]["items"]) == 2
    log = rules["log_problems"]["items"]
    # Three rows that differ only in the community are one problem.
    assert len(log) == 1
    assert log[0]["title"] == "Session cookie invalid"
    assert log[0]["detail"] == "3× за сутки (ingest): c0, c1, c2"
    assert log[0]["link"] == {"section": "log", "q": "Session cookie invalid"}


def test_plaintext_cookies(app):
    client, db, app_ = app
    with db.session() as s:
        session_for(s, "a.circle.so", plaintext=True)
        session_for(s, "b.circle.so")
    _clear_cache(app_)
    items = _rules(client)["plaintext_cookies"]["items"]
    assert len(items) == 1 and items[0]["detail"] == "a.circle.so"


# --- marking as seen ---------------------------------------------------------------

def _dead_session(db, detail="Session cookie invalid"):
    with db.session() as s:
        row = s.query(__import__("circle_leads.storage.models", fromlist=["CircleConnection"])
                      .CircleConnection).filter_by(host="dead.circle.so").one_or_none()
        if row is None:
            connection(s, "dead.circle.so", "session_expired", detail)
        else:
            row.state, row.state_detail = "session_expired", detail


def _item(client):
    items = _rules(client)["session_dead"]["items"]
    return items[0] if items else None


def test_a_mark_hides_the_item_until_its_state_changes(app):
    client, db, app_ = app
    _dead_session(db)
    _clear_cache(app_)
    item = _item(client)
    assert client.get("/api/dash/attention").json()["open"] == 1

    r = client.post("/api/dash/attention/ack", json={
        "rule": "session_dead", "item_key": item["key"], "fingerprint": item["fp"]})
    assert r.status_code == 200
    data = client.get("/api/dash/attention").json()
    assert data["open"] == 0
    assert _item(client)["acked"] is True

    # The same session fails in a different way: that is news.
    _dead_session(db, detail="Session rejected -- refresh the cookie.")
    _clear_cache(app_)
    assert _item(client)["acked"] is False
    assert client.get("/api/dash/attention").json()["open"] == 1


def test_a_mark_lets_go_once_the_problem_clears(app):
    client, db, app_ = app
    _dead_session(db)
    _clear_cache(app_)
    item = _item(client)
    client.post("/api/dash/attention/ack", json={
        "rule": "session_dead", "item_key": item["key"], "fingerprint": item["fp"]})
    with db.session() as s:
        from circle_leads.storage.models import AttentionAck, CircleConnection
        s.query(CircleConnection).filter_by(host="dead.circle.so").one().state = "connected"
    _clear_cache(app_)
    assert _item(client) is None
    with db.session() as s:
        assert s.query(AttentionAck).count() == 0
    # The next time it breaks the same way, it shows again.
    _dead_session(db)
    _clear_cache(app_)
    assert _item(client)["acked"] is False


def test_unmarking_and_bad_input(app):
    client, db, app_ = app
    _dead_session(db)
    _clear_cache(app_)
    item = _item(client)
    client.post("/api/dash/attention/ack", json={
        "rule": "session_dead", "item_key": item["key"], "fingerprint": item["fp"]})
    client.post("/api/dash/attention/unack", json={"rule": "session_dead", "item_key": item["key"]})
    assert _item(client)["acked"] is False
    assert client.post("/api/dash/attention/ack", json={
        "rule": "nope", "item_key": "x", "fingerprint": "y"}).status_code == 400


def test_a_broken_rule_is_an_item_not_a_500(app, monkeypatch):
    client, _db, app_ = app
    from circle_leads.web.sections import attention

    def boom(ctx):
        raise RuntimeError("rule exploded")

    monkeypatch.setattr(attention.RULES[0], "fn", boom)
    _clear_cache(app_)
    rules = _rules(client)
    assert "rule exploded" in rules[attention.RULES[0].key]["error"]
    assert all(r["error"] is None for k, r in rules.items() if k != attention.RULES[0].key)
