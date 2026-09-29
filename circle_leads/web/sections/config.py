"""Section 6: what the system is set to, and how it is doing.

Editable: the four schedules and the lead rules the worker actually reads.
Read-only: the services' heartbeats and snapshots, each stage's last run,
finish and error, and the last jobs.

Two things the old config tab did that this one does not:
- a save replaced the whole stored config with whatever the form sent, so a
  field the form did not show was reset to its default on every save. A save
  now merges into what is stored;
- a save started a full re-classification in a thread inside the web request
  (a 60-second function on Vercel), which marked every post unclassified
  first. Nothing is started by a save now.
"""

from __future__ import annotations

import copy
import json
import os
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from circle_leads.config.settings import requirements_to_dict, validate_requirements
from circle_leads.storage.activity import log_activity
from circle_leads.storage.heartbeat import HEARTBEAT_LIMITS_S
from circle_leads.storage.models import ScanJob, Setting
from circle_leads.storage import settings_store as store
from circle_leads.web.sections.deps import get_db, iso, require_session, utc_now

router = APIRouter(prefix="/api/dash", dependencies=[Depends(require_session)])

# Requirement fields the worker reads, and so the only ones worth editing.
# Hidden because nothing reads them (checked 2026-09-29): exclude_job_seekers,
# keywords.include, harvest_recency_days and harvest_all_spaces (only the old
# dashboard's manual harvest), retention_days (only `purge --expired`),
# join_persona (read nowhere), join_pacing (auto-join reads the YAML file).
# Locked: excluded_content and rate_limit are safety settings.
EDITABLE = {
    "target_roles": None,
    "target_skills": None,
    "minimum_confidence": None,
    "max_post_age_days": None,
    "max_search_niches": None,
    "expand_search_with_ai": None,
    "max_expanded_niches": None,
    "llm_escalation_threshold": None,
    "icp_escalation_threshold": None,
    "keywords": {"exclude"},
    "scoring": {"hiring_intent", "target_role_match", "target_skill_match",
                "budget_mentioned", "company_identified", "recent_post", "recency_days"},
    "priority_thresholds": {"high", "medium"},
}

SCHEDULES = {
    # key: (setting, default, setter, options)
    "harvest": (store.KEY_SCHEDULE, store.DEFAULT_SCHEDULE, store.set_schedule,
                sorted(store._NAMED_SCHEDULES)),
    "discovery": (store.KEY_DISCOVERY, store.DEFAULT_DISCOVERY, store.set_discovery_schedule,
                  sorted(store._DISCOVERY_SCHEDULES)),
    "icp_classification": (store.KEY_ICP, store.DEFAULT_ICP_SCHEDULE, store.set_icp_schedule,
                           sorted(store._ICP_SCHEDULES)),
    "join_type": (store.KEY_JOIN_TYPE, store.DEFAULT_JOIN_TYPE_SCHEDULE,
                  store.set_join_type_schedule, sorted(store._JOIN_TYPE_SCHEDULES)),
}

# The work the dashboard may put in the worker's queue. Directory discovery
# needs a browser the server does not have, so it is not offered.
JOB_KINDS = ("harvest", "scan_all")


def _settings(s: Session, keys: list[str]) -> dict[str, tuple[str | None, datetime | None]]:
    return {k: (v, at) for k, v, at in s.execute(
        select(Setting.key, Setting.value, Setting.updated_at).where(Setting.key.in_(keys))
    ).all()}


def _json(raw: str | None) -> Any:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return {"unreadable": raw[:200]}


def _age_s(now: datetime, raw: str | None) -> float | None:
    if not raw:
        return None
    try:
        stamp = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if stamp.tzinfo is not None:
        stamp = stamp.replace(tzinfo=None)
    return (now - stamp).total_seconds()


def _stamp(raw: str | None) -> str | None:
    """A stored ISO stamp (naive UTC) as an explicit UTC string."""
    if not raw:
        return None
    try:
        return iso(datetime.fromisoformat(raw))
    except ValueError:
        return raw


def _snapshot(raw: str | None) -> Any:
    """A service's snapshot with its times as explicit UTC (stored naive)."""
    snap = _json(raw)
    if isinstance(snap, dict):
        for key in ("started_at", "beat_at"):
            snap[key] = _stamp(snap.get(key))
        stage = snap.get("stage")
        if isinstance(stage, dict) and stage.get("since"):
            stage["since"] = _stamp(stage["since"])
    return snap


def build_runtime(s: Session, now: datetime) -> dict[str, Any]:
    keys = [
        *HEARTBEAT_LIMITS_S, "worker_runtime", "watcher_runtime", "enrichment_cursor_id",
        store.KEY_LAST_RUN, store.KEY_DISCOVERY_LAST_RUN, store.KEY_ICP_LAST_RUN,
        store.KEY_JOIN_TYPE_LAST_RUN,
        *[k for _, (k, *_rest) in SCHEDULES.items()],
        *[f"{stage}_{suffix}" for stage in ("harvest", "icp_classification", "join_type")
          for suffix in ("last_finish", "last_error", "last_result")],
    ]
    vals = _settings(s, keys)

    def value(key: str) -> str | None:
        return (vals.get(key) or (None, None))[0]

    heartbeats = {}
    for key, limit in HEARTBEAT_LIMITS_S.items():
        raw = value(key)
        age = _age_s(now, raw)
        state = "never" if not raw else "unreadable" if age is None else (
            "ok" if age <= limit else "stale")
        heartbeats[key.replace("_heartbeat", "")] = {
            "at": _stamp(raw), "age_s": round(age) if age is not None else None,
            "limit_s": limit, "state": state,
        }

    def stage(name: str, last_run_key: str | None) -> dict[str, Any]:
        setting, default, *_ = SCHEDULES[name]
        error = _json(value(f"{name}_last_error"))
        if isinstance(error, dict) and error.get("at"):
            error["at"] = _stamp(error["at"])
        return {
            "schedule": value(setting) or default,
            "last_run": _stamp(value(last_run_key)) if last_run_key else None,
            "last_finish": _stamp(value(f"{name}_last_finish")),
            "last_error": error,
            "last_result": _json(value(f"{name}_last_result")),
        }

    cursor = vals.get("enrichment_cursor_id") or (None, None)
    stages = {
        "harvest": stage("harvest", store.KEY_LAST_RUN),
        "discovery": {"schedule": value(store.KEY_DISCOVERY) or store.DEFAULT_DISCOVERY,
                      "last_run": _stamp(value(store.KEY_DISCOVERY_LAST_RUN))},
        "icp_classification": {**stage("icp_classification", store.KEY_ICP_LAST_RUN),
                               "enrichment_cursor": cursor[0],
                               "enrichment_moved_at": iso(cursor[1])},
        "join_type": stage("join_type", store.KEY_JOIN_TYPE_LAST_RUN),
    }
    jobs = [{
        "id": j.id, "kind": j.kind, "host": j.host, "state": j.state, "detail": j.detail,
        "created_at": iso(j.created_at), "claimed_at": iso(j.claimed_at),
        "finished_at": iso(j.finished_at), "attempts": j.attempts,
    } for j in s.scalars(select(ScanJob).order_by(ScanJob.id.desc()).limit(20)).all()]

    return {
        "generated_at": iso(now),
        "heartbeats": heartbeats,
        "runtime": {"worker": _snapshot(value("worker_runtime")),
                    "watcher": _snapshot(value("watcher_runtime"))},
        "stages": stages,
        "jobs": jobs,
        "dashboard": {
            "commit": (os.environ.get("VERCEL_GIT_COMMIT_SHA") or "")[:7] or None,
            "region": os.environ.get("VERCEL_REGION"),
            "env": os.environ.get("VERCEL_ENV"),
        },
    }


def merge_requirements(stored: dict, payload: dict) -> dict:
    """``stored`` with the editable fields of ``payload`` laid over it.

    Anything the form does not send keeps its stored value; anything it may
    not edit is ignored.
    """
    out = copy.deepcopy(stored)
    for key, value in (payload or {}).items():
        if key not in EDITABLE:
            continue
        subkeys = EDITABLE[key]
        if subkeys is None:
            out[key] = value
            continue
        if not isinstance(value, dict):
            raise ValueError(f"{key} must be an object")
        out[key] = {**out.get(key, {}), **{k: v for k, v in value.items() if k in subkeys}}
    return out


def _stored_requirements(request: Request):
    return store.load_effective_requirements(request.app.state.db,
                                             request.app.state.config_path)


@router.get("/runtime")
def api_runtime(db=Depends(get_db), now: datetime = Depends(utc_now)) -> dict[str, Any]:
    with db.session() as s:
        return build_runtime(s, now)


@router.post("/jobs")
def api_enqueue_job(payload: dict, db=Depends(get_db)) -> dict[str, Any]:
    from circle_leads.storage.job_queue import enqueue

    kind = str((payload or {}).get("kind") or "")
    if kind not in JOB_KINDS:
        raise HTTPException(400, f"kind must be one of {list(JOB_KINDS)}")
    job_id = enqueue(db, kind, priority=0)
    with db.session() as s:
        log_activity(s, kind="review", summary=f"Queued {kind} from the dashboard")
    return {"ok": True, "job_id": job_id}


@router.get("/schedule")
def api_schedule(db=Depends(get_db)) -> dict[str, Any]:
    with db.session() as s:
        vals = _settings(s, [setting for setting, *_ in SCHEDULES.values()])
    return {
        name: {"value": (vals.get(setting) or (None, None))[0] or default,
               "default": default, "options": options}
        for name, (setting, default, _setter, options) in SCHEDULES.items()
    }


@router.post("/schedule")
def api_schedule_save(payload: dict, db=Depends(get_db)) -> dict[str, Any]:
    changed = {}
    for name, value in (payload or {}).items():
        if name not in SCHEDULES:
            raise HTTPException(400, f"Unknown schedule {name!r}")
        setter = SCHEDULES[name][2]
        try:
            setter(db, str(value))
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        changed[name] = str(value)
    if changed:
        with db.session() as s:
            log_activity(s, kind="review", summary="Schedules changed: " + ", ".join(
                f"{name}={value}" for name, value in changed.items()))
    return {"ok": True, "changed": changed}


@router.get("/config")
def api_config(request: Request) -> dict[str, Any]:
    # Read fresh: another Vercel instance may have saved since this one started.
    data = requirements_to_dict(_stored_requirements(request))
    return {"config": data, "editable": {k: sorted(v) if v else None
                                         for k, v in EDITABLE.items()}}


@router.post("/config")
def api_config_save(request: Request, payload: dict) -> dict[str, Any]:
    stored = requirements_to_dict(_stored_requirements(request))
    try:
        merged = merge_requirements(stored, payload)
        new_req = validate_requirements(merged)
    except Exception as exc:  # noqa: BLE001 - the form shows the message
        raise HTTPException(400, f"Invalid config: {exc}") from exc
    canonical = requirements_to_dict(new_req)
    store.set_requirements_override(request.app.state.db, canonical)
    request.app.state.requirements_holder["req"] = new_req
    changed = sorted(k for k in canonical if canonical[k] != stored.get(k))
    with request.app.state.db.session() as s:
        log_activity(s, kind="review", summary="Lead requirements updated via dashboard",
                     detail={"changed": ", ".join(changed) or "nothing"})
    return {"ok": True, "config": canonical, "changed": changed}
