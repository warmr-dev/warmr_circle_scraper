"""Section 6: schedules, lead rules, the services' state, the job buttons.

The old config tab replaced the whole stored config with whatever the form
sent, so every field the form did not show fell back to its default on each
save, and each save started a full re-classification inside the web request.
"""

import json

from circle_leads.config.settings import requirements_to_dict
from circle_leads.storage.models import ScanJob
from circle_leads.storage.settings_store import get_requirements_override, set_requirements_override
from tests.dash_fixtures import NOW, make_app, setting


def test_a_save_keeps_every_field_the_form_did_not_send(tmp_path, monkeypatch):
    client, db, app = make_app(tmp_path, monkeypatch)
    stored = client.get("/api/dash/config").json()["config"]
    stored["max_search_niches"] = 7
    stored["join_persona"]["company_name"] = "Rapid Dev"
    stored["scoring"]["recency_days"] = 3
    set_requirements_override(db, stored)

    r = client.post("/api/dash/config", json={"target_roles": ["CTO"],
                                              "scoring": {"hiring_intent": 45}})
    assert r.status_code == 200, r.text
    saved = get_requirements_override(db)
    assert saved["target_roles"] == ["CTO"]
    assert saved["scoring"]["hiring_intent"] == 45
    # Untouched by the form, so untouched by the save:
    assert saved["max_search_niches"] == 7
    assert saved["join_persona"]["company_name"] == "Rapid Dev"
    assert saved["scoring"]["recency_days"] == 3
    assert r.json()["changed"] == ["scoring", "target_roles"]
    # The running instance uses the new rules at once.
    assert app.state.requirements_holder["req"].target_roles == ["CTO"]


def test_fields_nothing_reads_cannot_be_edited(tmp_path, monkeypatch):
    client, db, _app = make_app(tmp_path, monkeypatch)
    before = client.get("/api/dash/config").json()["config"]
    r = client.post("/api/dash/config", json={
        "harvest_all_spaces": not before["harvest_all_spaces"],
        "retention_days": 1,
        "rate_limit": {"requests_per_minute": 999},
        "keywords": {"include": ["x"], "exclude": ["job seeker"]},
    })
    assert r.status_code == 200
    after = get_requirements_override(db)
    assert after["harvest_all_spaces"] == before["harvest_all_spaces"]
    assert after["retention_days"] == before["retention_days"]
    assert after["rate_limit"] == before["rate_limit"]
    assert after["keywords"]["include"] == before["keywords"]["include"]
    assert after["keywords"]["exclude"] == ["job seeker"]


def test_an_invalid_value_is_refused_and_nothing_is_written(tmp_path, monkeypatch):
    client, db, _app = make_app(tmp_path, monkeypatch)
    r = client.post("/api/dash/config", json={"minimum_confidence": 5})
    assert r.status_code == 400
    assert get_requirements_override(db) is None


def test_a_save_no_longer_starts_a_reclassify(tmp_path, monkeypatch):
    client, _db, _app = make_app(tmp_path, monkeypatch)
    assert client.post("/api/jobs/reclassify", json={}).status_code in (404, 405)


def test_the_four_schedules(tmp_path, monkeypatch):
    client, _db, _app = make_app(tmp_path, monkeypatch)
    schedules = client.get("/api/dash/schedule").json()
    assert set(schedules) == {"harvest", "discovery", "icp_classification", "join_type"}
    assert schedules["icp_classification"]["value"] == "hourly"
    r = client.post("/api/dash/schedule", json={"icp_classification": "every_6h",
                                                "join_type": "daily"})
    assert r.status_code == 200
    after = client.get("/api/dash/schedule").json()
    assert (after["icp_classification"]["value"], after["join_type"]["value"]) == ("every_6h", "daily")
    assert client.post("/api/dash/schedule", json={"join_type": "sometimes"}).status_code == 400
    assert client.post("/api/dash/schedule", json={"nope": "daily"}).status_code == 400


def test_the_services_and_their_stages(tmp_path, monkeypatch):
    client, db, _app = make_app(tmp_path, monkeypatch)
    with db.session() as s:
        setting(s, "worker_heartbeat", NOW.isoformat())
        setting(s, "worker_runtime", json.dumps({
            "host": "box", "env": {"VINI_API_SECRET": True}, "beat_at": "2026-09-29T10:59:00",
            "stage": {"stage": "harvest", "since": "2026-09-29T09:00:00"}}))
        setting(s, "harvest_last_run", NOW.isoformat())
        setting(s, "join_type_last_error", json.dumps({"at": NOW.isoformat(), "error": "boom"}))
    r = client.get("/api/dash/runtime").json()
    assert r["heartbeats"]["worker"]["state"] == "ok"
    assert r["heartbeats"]["watcher"]["state"] == "never"
    assert r["runtime"]["worker"]["host"] == "box"
    # Stored naive, sent as explicit UTC, or the browser shifts them by its offset.
    assert r["runtime"]["worker"]["beat_at"] == "2026-09-29T10:59:00Z"
    assert r["runtime"]["worker"]["stage"]["since"] == "2026-09-29T09:00:00Z"
    assert r["stages"]["harvest"]["last_run"] == "2026-09-29T11:00:00Z"
    assert r["stages"]["join_type"]["last_error"] == {"at": "2026-09-29T11:00:00Z", "error": "boom"}


def test_the_job_buttons_queue_work_for_the_worker(tmp_path, monkeypatch):
    client, db, _app = make_app(tmp_path, monkeypatch)
    first = client.post("/api/dash/jobs", json={"kind": "harvest"}).json()
    again = client.post("/api/dash/jobs", json={"kind": "harvest"}).json()
    assert first["job_id"] == again["job_id"]  # one queued harvest, not two
    assert client.post("/api/dash/jobs", json={"kind": "discover_directory"}).status_code == 400
    with db.session() as s:
        assert [(j.kind, j.state) for j in s.query(ScanJob).all()] == [("harvest", "queued")]


def test_requirements_round_trip_through_the_canonical_form(tmp_path, monkeypatch):
    client, _db, app = make_app(tmp_path, monkeypatch)
    before = client.get("/api/dash/config").json()["config"]
    r = client.post("/api/dash/config", json={})
    assert r.status_code == 200 and r.json()["changed"] == []
    assert r.json()["config"] == before == requirements_to_dict(app.state.requirements_holder["req"])
