"""Background jobs for dashboard-triggered searches and feed reads.

A search or a feed read takes minutes, so the dashboard kicks one off as a
background job and polls its status. Jobs run in a thread; a small in-memory
registry tracks their state so the UI can show progress. State is per-process
and resets on restart -- these are convenience triggers, not durable tasks.
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable


@dataclass
class Job:
    id: str
    kind: str  # "search" | "read"
    label: str
    state: str = "running"  # running | done | error
    detail: str = ""
    result: dict = field(default_factory=dict)
    started_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    finished_at: str | None = None

    def as_dict(self) -> dict:
        return {
            "id": self.id, "kind": self.kind, "label": self.label,
            "state": self.state, "detail": self.detail, "result": self.result,
            "started_at": self.started_at, "finished_at": self.finished_at,
        }


class JobRegistry:
    """Thread-safe registry of background jobs, newest first."""

    def __init__(self, max_jobs: int = 50):
        self._jobs: dict[str, Job] = {}
        self._order: list[str] = []
        self._lock = threading.Lock()
        self._max = max_jobs

    def start(self, kind: str, label: str, target: Callable[["Job"], None]) -> Job:
        """Register a job and run ``target(job)`` in a daemon thread."""
        job = Job(id=uuid.uuid4().hex[:12], kind=kind, label=label)
        with self._lock:
            self._jobs[job.id] = job
            self._order.insert(0, job.id)
            # Trim old finished jobs.
            while len(self._order) > self._max:
                old = self._order.pop()
                self._jobs.pop(old, None)

        def run():
            try:
                target(job)
                if job.state == "running":
                    job.state = "done"
            except Exception as exc:  # noqa: BLE001 - surface any failure to the UI
                job.state = "error"
                job.detail = f"{exc.__class__.__name__}: {exc}"
            finally:
                job.finished_at = datetime.now(timezone.utc).isoformat()

        threading.Thread(target=run, daemon=True).start()
        return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def list(self, limit: int = 20) -> list[dict]:
        with self._lock:
            return [self._jobs[i].as_dict() for i in self._order[:limit]]

    def active(self, kind: str | None = None) -> bool:
        with self._lock:
            return any(
                j.state == "running" and (kind is None or j.kind == kind)
                for j in self._jobs.values()
            )
