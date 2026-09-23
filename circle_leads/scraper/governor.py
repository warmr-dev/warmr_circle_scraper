"""One request budget to Circle for every process on this machine.

Circle's Cloudflare limits requests per IP across every community, custom
domains included. Measured 2026-09-18 from a home Mac: ~100 req/min earned
HTTP 429 + ``cf-mitigated: challenge`` on every Circle host for ~15 minutes.
Before this module each ``PublicReader`` throttled only itself (a fresh reader
per community reset the clock) and the desktop app kept a separate budget, so
two jobs on one Mac could together blow straight past the limit.

The budget now lives in one small JSON file that every process on the machine
shares -- the Python CLI, ad-hoc scripts and the Electron app
(``desktop/src/engine/circle/governor.ts`` speaks the same protocol):

* a lock directory next to the file (``mkdir`` is atomic in both languages);
* ``nextAt`` -- the earliest start of the next request, so the pace is shared;
* ``hour`` -- start times in the last hour, for the hourly ceiling;
* ``cooldownUntil`` -- set by whoever sees Circle push back; everyone pauses;
* ``highSeenAt`` / ``lowNextAt`` -- reads that can produce leads are "high"
  priority; name/join-type probes are "low" and only get every other slot
  while a high-priority job is active, so enrichment can't starve reading.

``limits`` in the file are the single setting (the desktop app writes its own
network settings there). Without a file, the defaults below apply.

Set ``CIRCLE_GOVERNOR=off`` to bypass (tests do; it is off by default on
Vercel, see ``enabled``). If the file can't be used
(read-only filesystem, e.g. a serverless function) the budget falls back to
this process only -- still a budget, never an error.
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import logging
import os
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

HIGH = "high"
LOW = "low"

DEFAULT_LIMITS = {"requestsPerMinute": 20, "maxPerHour": 600, "cooldownMinutes": 60}

#: A high-priority request this recent means a reading job is running.
HIGH_ACTIVE_MS = 30_000
#: A lock older than this belongs to a crashed process.
STALE_LOCK_MS = 10_000

# Hosts on the shared session that are not Circle: search APIs, LLMs, our own
# database and portal. Everything else a Circle reader touches (subdomains and
# custom domains alike) is budgeted.
NOT_CIRCLE_SUFFIXES = (
    "openrouter.ai", "openai.com", "anthropic.com", "supabase.co", "supabase.com",
    "exa.ai", "serper.dev", "tavily.com", "brave.com", "googleapis.com",
    "bing.microsoft.com", "duckduckgo.com", "netlas.io", "vercel.app",
    "railway.app",
)

_priority: contextvars.ContextVar[str] = contextvars.ContextVar(
    "circle_request_priority", default=HIGH
)


@contextlib.contextmanager
def priority(level: str):
    """Run a block's Circle requests at ``level`` (``HIGH`` or ``LOW``).

    Context variables don't cross into ThreadPoolExecutor workers on their
    own -- call this inside the worker function, not around ``pool.map``.
    """
    token = _priority.set(level)
    try:
        yield
    finally:
        _priority.reset(token)


def is_circle_host(host: str | None) -> bool:
    h = (host or "").lower().rstrip(".")
    if not h or h in ("localhost", "127.0.0.1"):
        return False
    return not any(h == s or h.endswith("." + s) for s in NOT_CIRCLE_SUFFIXES)


class CircleRateLimited(Exception):
    """Circle is cooling us down and the caller asked not to wait that long."""

    def __init__(self, until_ms: float, reason: str = ""):
        self.until_ms = until_ms
        self.reason = reason
        left = max(0, int((until_ms - time.time() * 1000) / 1000))
        super().__init__(f"Circle rate limit: paused for {left}s more ({reason})")


def _now_ms() -> float:
    return time.time() * 1000


def default_state_path() -> Path:
    env = os.environ.get("CIRCLE_GOVERNOR_FILE")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".warmr" / "circle-governor.json"


@dataclass
class Grant:
    wait_s: float           # sleep this long, then send
    cooldown_until: float   # >0: no grant, Circle is cooling us down (ms epoch)
    reason: str = ""


class CircleGovernor:
    """File-backed shared budget. Thread-safe within a process."""

    def __init__(self, path: Path | None = None):
        self.path = path or default_state_path()
        self._thread_lock = threading.Lock()
        self._memory: dict = {}          # fallback state when the file is unusable
        self._file_ok: bool | None = None

    # -- storage -----------------------------------------------------------
    @property
    def _lock_dir(self) -> Path:
        return self.path.with_name(self.path.name + ".lock")

    def _usable(self) -> bool:
        if self._file_ok is None:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                probe = self.path.parent / f".probe-{os.getpid()}"
                probe.write_text("")
                probe.unlink()
                self._file_ok = True
            except OSError as exc:
                logger.warning("Circle governor file unusable (%s); per-process budget only", exc)
                self._file_ok = False
        return self._file_ok

    @contextlib.contextmanager
    def _locked(self):
        with self._thread_lock:
            if not self._usable():
                yield None
                return
            deadline = time.monotonic() + 5
            while True:
                try:
                    os.mkdir(self._lock_dir)
                    break
                except FileExistsError:
                    try:
                        age = _now_ms() - self._lock_dir.stat().st_mtime * 1000
                        if age > STALE_LOCK_MS:
                            os.rmdir(self._lock_dir)
                            continue
                    except OSError:
                        continue
                    if time.monotonic() > deadline:
                        # A wedged lock must not stop the whole machine's traffic;
                        # take it over (the stale check above normally fires first).
                        with contextlib.suppress(OSError):
                            os.rmdir(self._lock_dir)
                        continue
                    time.sleep(0.02)
                except OSError as exc:
                    logger.warning("Circle governor lock failed (%s); per-process budget only", exc)
                    self._file_ok = False
                    yield None
                    return
            try:
                yield self.path
            finally:
                with contextlib.suppress(OSError):
                    os.rmdir(self._lock_dir)

    def _read(self, path: Path | None) -> dict:
        if path is None:
            return dict(self._memory)
        try:
            data = json.loads(path.read_text())
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _write(self, path: Path | None, state: dict) -> None:
        if path is None:
            self._memory = state
            return
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".gov-")
        try:
            with os.fdopen(fd, "w") as fh:
                json.dump(state, fh)
            os.replace(tmp, path)
        except OSError:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            self._memory = state

    @staticmethod
    def _limits(state: dict) -> dict:
        limits = dict(DEFAULT_LIMITS)
        raw = state.get("limits")
        if isinstance(raw, dict):
            for key in DEFAULT_LIMITS:
                val = raw.get(key)
                if isinstance(val, (int, float)) and val >= 0:
                    limits[key] = val
        return limits

    # -- budget ------------------------------------------------------------
    def _reserve(self, level: str) -> Grant:
        with self._locked() as path:
            state = self._read(path)
            now = _now_ms()
            cooldown = float(state.get("cooldownUntil") or 0)
            if cooldown > now:
                return Grant(0, cooldown, str(state.get("cooldownReason") or ""))
            limits = self._limits(state)
            interval = 60_000 / max(1, limits["requestsPerMinute"])
            hour = [t for t in state.get("hour") or [] if isinstance(t, (int, float)) and t > now - 3_600_000]
            slot = max(now, float(state.get("nextAt") or 0))
            if level == LOW and now - float(state.get("highSeenAt") or 0) < HIGH_ACTIVE_MS:
                slot = max(slot, float(state.get("lowNextAt") or 0))
            max_hour = int(limits["maxPerHour"])
            if max_hour > 0 and len(hour) >= max_hour:
                slot = max(slot, hour[len(hour) - max_hour] + 3_600_000 + 50)
            state["nextAt"] = slot + interval
            if level == LOW:
                state["lowNextAt"] = slot + 2 * interval
            else:
                state["highSeenAt"] = now
            hour.append(slot)
            state["hour"] = hour
            state["limits"] = limits
            self._write(path, state)
            return Grant(max(0.0, (slot - now) / 1000), 0)

    def acquire(self, *, max_wait_s: float | None = None) -> None:
        """Block until this process may send one request to Circle.

        While Circle is cooling us down, waits it out (a batch job would only
        record false "dead" rows otherwise) unless the remaining pause exceeds
        ``max_wait_s``, in which case ``CircleRateLimited`` is raised.
        """
        level = _priority.get()
        while True:
            grant = self._reserve(level)
            if not grant.cooldown_until:
                if grant.wait_s > 0:
                    time.sleep(grant.wait_s)
                return
            left = (grant.cooldown_until - _now_ms()) / 1000
            if max_wait_s is not None and left > max_wait_s:
                raise CircleRateLimited(grant.cooldown_until, grant.reason)
            logger.warning("Circle is rate-limiting this IP (%s); pausing %.0fs", grant.reason, left)
            time.sleep(min(max(left, 1.0), 300.0))

    def trip(self, reason: str) -> float:
        """Circle pushed back: pause every process on this machine."""
        with self._locked() as path:
            state = self._read(path)
            limits = self._limits(state)
            until = _now_ms() + max(1, limits["cooldownMinutes"]) * 60_000
            if until > float(state.get("cooldownUntil") or 0):
                state["cooldownUntil"] = until
                state["cooldownReason"] = reason[:200]
                self._write(path, state)
            logger.warning("Circle rate limit tripped: %s", reason)
            return float(state.get("cooldownUntil") or until)

    def status(self) -> dict:
        with self._locked() as path:
            state = self._read(path)
        now = _now_ms()
        return {
            "limits": self._limits(state),
            "usedLastHour": len([t for t in state.get("hour") or [] if isinstance(t, (int, float)) and now - 3_600_000 < t <= now]),
            "cooldownUntil": state.get("cooldownUntil") if float(state.get("cooldownUntil") or 0) > now else None,
            "cooldownReason": state.get("cooldownReason"),
            "path": str(self.path),
        }


_governor: CircleGovernor | None = None
_governor_lock = threading.Lock()


def enabled() -> bool:
    # Off by default on Vercel: the dashboard calls Circle inside a request a
    # person is waiting on, from Vercel's own IPs, not from the Mac's budget.
    default = "off" if os.environ.get("VERCEL") else "on"
    return os.environ.get("CIRCLE_GOVERNOR", default).strip().lower() not in ("off", "0", "false", "no")


def get_governor() -> CircleGovernor:
    global _governor
    with _governor_lock:
        if _governor is None or _governor.path != default_state_path():
            _governor = CircleGovernor()
        return _governor


def max_wait_from_env() -> float | None:
    raw = os.environ.get("CIRCLE_GOVERNOR_MAX_WAIT")
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None
