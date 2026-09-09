"""Durable scan-job queue (Supabase table), shared by dashboard and worker.

The dashboard enqueues; the always-on worker claims and runs. Claiming is
atomic (a conditional UPDATE) so concurrent workers never run the same job.
This keeps heavy work off the web request path -- the UI just writes a row.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update

from circle_leads.storage.database import Database
from circle_leads.storage.models import JobState, ScanJob


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def enqueue(db: Database, kind: str, *, host: str | None = None,
            priority: int = 1) -> int:
    """Add a job, unless an identical one is already queued/running (dedup)."""
    with db.session() as s:
        existing = s.scalar(
            select(ScanJob).where(
                ScanJob.kind == kind,
                ScanJob.host == host,
                ScanJob.state.in_([JobState.QUEUED.value, JobState.RUNNING.value]),
            )
        )
        if existing is not None:
            return existing.id
        job = ScanJob(kind=kind, host=host, priority=priority)
        s.add(job)
        s.flush()
        return job.id


def claim_next(db: Database) -> ScanJob | None:
    """Atomically claim the highest-priority queued job. Returns a detached
    copy the caller can run, or None if the queue is empty."""
    with db.session() as s:
        # Re-queue jobs stuck RUNNING for >15 min (a worker died mid-job).
        stale = _utcnow() - timedelta(minutes=15)
        s.execute(
            update(ScanJob)
            .where(ScanJob.state == JobState.RUNNING.value,
                   ScanJob.claimed_at < stale)
            .values(state=JobState.QUEUED.value)
        )
        candidate = s.scalar(
            select(ScanJob)
            .where(ScanJob.state == JobState.QUEUED.value)
            .order_by(ScanJob.priority, ScanJob.created_at)
        )
        if candidate is None:
            return None
        # Conditional claim: only succeeds if still queued (race-safe).
        res = s.execute(
            update(ScanJob)
            .where(ScanJob.id == candidate.id,
                   ScanJob.state == JobState.QUEUED.value)
            .values(state=JobState.RUNNING.value, claimed_at=_utcnow(),
                    attempts=ScanJob.attempts + 1)
        )
        if res.rowcount != 1:
            return None  # someone else claimed it; try again next loop
        s.flush()
        s.refresh(candidate)
        s.expunge(candidate)
        return candidate


def complete(db: Database, job_id: int, *, result: dict, error: str | None = None) -> None:
    with db.session() as s:
        job = s.get(ScanJob, job_id)
        if job is None:
            return
        job.state = JobState.ERROR.value if error else JobState.DONE.value
        job.result = result or {}
        job.detail = error or (result.get("detail") if result else None)
        job.finished_at = _utcnow()


def recent(db: Database, limit: int = 20) -> list[dict]:
    with db.session() as s:
        rows = s.scalars(
            select(ScanJob).order_by(ScanJob.created_at.desc()).limit(limit)
        ).all()
        return [{
            "id": j.id, "kind": j.kind, "host": j.host, "state": j.state,
            "detail": j.detail, "result": j.result,
            "created_at": j.created_at.isoformat() if j.created_at else None,
            "finished_at": j.finished_at.isoformat() if j.finished_at else None,
        } for j in rows]
