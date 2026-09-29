"""What a running service is, written where the dashboard can read it.

The dashboard runs on Vercel and sees only the database: not the worker's
environment, not its log files, not the rate-limit file on its disk. So a
service writes a snapshot of itself next to its heartbeat -- which host, which
code, which LLM, which keys are set (as booleans, never values), the Circle
request budget, and the stage it is in right now.

The stage matters on its own: ``harvest_last_run`` is stamped when a harvest
starts, so "started and never finished" is also what a harvest looks like for
the hours it runs. Only the service can say which of the two it is.
"""

from __future__ import annotations

import logging
import os
import platform
import re
import socket
import subprocess
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# Keys whose presence decides what a service can do. Only "set / not set"
# leaves the process; the values never do.
ENV_FLAGS = (
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "OPENROUTER_API_KEY",
    "VINI_API_SECRET",
    "SUPABASE_ANON_KEY",
    "COMMUNITY_INTAKE_API_SECRET",
    "EXA_API_KEY",
    "BRAVE_API_KEY",
    "SERPAPI_API_KEY",
    "CIRCLE_CRED_KEY",
    "CIRCLE_MAIL_USER",
    "CIRCLE_MAIL_APP_PASSWORD",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
)

_current: dict = {"stage": None, "since": None}
_lock = threading.Lock()


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat()


@contextmanager
def stage(name: str) -> Iterator[None]:
    """Mark the work inside as the service's current stage."""
    with _lock:
        previous = dict(_current)
        _current.update(stage=name, since=_now_iso())
    try:
        yield
    finally:
        with _lock:
            _current.update(previous)


def current_stage() -> dict:
    with _lock:
        return dict(_current)


def code_version() -> str:
    """The commit this process runs, as far as it can tell.

    WARMR_CODE_VERSION when the deploy sets it; otherwise the ``releases/<sha>``
    directory the droplet and openclaw layouts install into; otherwise git.
    """
    explicit = (os.environ.get("WARMR_CODE_VERSION") or "").strip()
    if explicit:
        return explicit[:64]
    match = re.search(r"/releases/([0-9a-f]{7,40})/", str(Path(__file__).resolve()))
    if match:
        return match.group(1)[:12]
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=Path(__file__).resolve().parent,
            capture_output=True, text=True, timeout=2,
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except Exception:  # noqa: BLE001 - no git on the host is normal
        pass
    return "unknown"


def _llm() -> dict:
    """The backend the classifier would pick now, without calling it."""
    try:
        from circle_leads.classifier.ai_classifier import make_backend

        backend = make_backend()
    except Exception as exc:  # noqa: BLE001
        return {"backend": None, "error": type(exc).__name__}
    if backend is None:
        return {"backend": None}
    base = os.environ.get("OPENAI_BASE_URL") or ""
    return {
        "backend": type(backend).__name__,
        "model": getattr(backend, "model", None),
        "base_host": urlparse(base).hostname if base else None,
    }


def static_snapshot(service: str) -> dict:
    """The part that does not change while the process lives."""
    return {
        "service": service,
        "host": socket.gethostname(),
        "pid": os.getpid(),
        "started_at": _now_iso(),
        "code_version": code_version(),
        "python": platform.python_version(),
        "llm": _llm(),
        "env": {name: bool((os.environ.get(name) or "").strip()) for name in ENV_FLAGS},
    }


def _governor() -> dict | None:
    try:
        from circle_leads.scraper import governor

        if not governor.enabled():
            return {"enabled": False}
        status = governor.get_governor().status()
        status.pop("path", None)
        return {"enabled": True, **status}
    except Exception as exc:  # noqa: BLE001 - a broken budget file is itself news
        return {"enabled": None, "error": type(exc).__name__}


def snapshot(static: dict) -> dict:
    """``static`` plus what changes: the stage, the budget, the time of the beat."""
    return {
        **static,
        "beat_at": _now_iso(),
        "stage": current_stage(),
        "governor": _governor(),
    }
