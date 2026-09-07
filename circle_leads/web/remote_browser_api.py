"""Dashboard routes for the server-hosted interactive browser (the PoC).

Every route requires the dashboard session, so the live view and its input
relay are never reachable anonymously. The browser is a single process-wide
singleton -- one Chromium, one profile, serialized by its own lock.

What deliberately does NOT exist here: any route that imports, uploads, or
exports cookies or session tokens. The session is created by you inside this
browser and stays in its profile directory.
"""

from __future__ import annotations

import os
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response

from circle_leads.remote_browser.probe import probe_authenticated, probe_environment
from circle_leads.remote_browser.session import (
    RemoteBrowser, RemoteBrowserUnavailable, SessionState,
)

# One browser per process. Created lazily so importing the app never starts
# Chromium (and so the test suite stays browser-free).
_browser: RemoteBrowser | None = None


def get_browser() -> RemoteBrowser:
    global _browser
    if _browser is None:
        _browser = RemoteBrowser(
            profile_dir=os.environ.get("REMOTE_BROWSER_PROFILE") or None,
            headless=True,
        )
    return _browser


def reset_browser() -> None:
    """Drop the singleton (used by tests)."""
    global _browser
    if _browser is not None and _browser.running:
        _browser.stop()
    _browser = None


def _enabled() -> bool:
    """The remote browser is opt-in: it only runs where explicitly enabled."""
    return os.environ.get("REMOTE_BROWSER_ENABLED", "").lower() == "true"


def build_replay_router(require_auth, db) -> APIRouter:
    """EXPERIMENT (Version B): store a captured Circle session and replay it
    from the server. Built at the user's explicit request, overriding the
    'no cookie replay' rule. Every route requires the dashboard session, and
    stored cookies are encrypted at rest (CIRCLE_CRED_KEY)."""
    from circle_leads.remote_browser.replay import (
        has_session_cookie, parse_cookies, replay_session,
    )
    from circle_leads.remote_browser.session import RemoteBrowserUnavailable
    from circle_leads.web.replay_store import (
        ReplayKeyMissing, delete_session, list_sessions, load_cookies,
        record_result, store_session,
    )

    router = APIRouter(prefix="/api/replay", tags=["replay-experiment"])

    @router.get("/sessions")
    def sessions(_: None = Depends(require_auth)) -> dict[str, Any]:
        return {"enabled": _enabled(), "sessions": list_sessions(db)}

    @router.post("/store")
    def store(payload: dict, _: None = Depends(require_auth)) -> dict[str, Any]:
        host = str((payload or {}).get("host") or "").strip().lower().replace("https://", "").strip("/")
        if not host or "." not in host:
            raise HTTPException(400, "A community host is required.")
        try:
            cookies = parse_cookies((payload or {}).get("cookies"))
        except (ValueError, TypeError) as exc:
            raise HTTPException(400, f"Invalid cookie export: {exc}") from exc
        if not has_session_cookie(cookies):
            raise HTTPException(
                400, "No Circle session cookie (_circle_session / "
                     "remember_user_token) found in the export.")
        try:
            res = store_session(db, host, cookies)
        except ReplayKeyMissing as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"ok": True, **res}

    @router.post("/test")
    def test(payload: dict, _: None = Depends(require_auth)) -> dict[str, Any]:
        """Replay the stored session for host and report the honest result."""
        host = str((payload or {}).get("host") or "").strip().lower().replace("https://", "").strip("/")
        try:
            cookies = load_cookies(db, host)
        except ReplayKeyMissing as exc:
            raise HTTPException(400, str(exc)) from exc
        if cookies is None:
            raise HTTPException(404, f"No stored session for {host}.")
        try:
            result = replay_session(host, cookies)
        except RemoteBrowserUnavailable as exc:
            raise HTTPException(503, str(exc)) from exc
        record_result(db, host, result.result, result.detail)
        return result.as_dict()

    @router.post("/delete")
    def delete(payload: dict, _: None = Depends(require_auth)) -> dict[str, Any]:
        host = str((payload or {}).get("host") or "").strip().lower().replace("https://", "").strip("/")
        delete_session(db, host)
        return {"ok": True}

    return router


def build_router(require_auth) -> APIRouter:
    router = APIRouter(prefix="/api/remote-browser", tags=["remote-browser"])

    def _require_enabled() -> None:
        if not _enabled():
            raise HTTPException(
                503,
                "The remote browser is disabled. Set REMOTE_BROWSER_ENABLED=true "
                "and deploy an image that includes Chromium.",
            )

    @router.get("/status")
    def status(_: None = Depends(require_auth)) -> dict[str, Any]:
        if not _enabled():
            return {"enabled": False, "state": SessionState.STOPPED.value,
                    "detail": "REMOTE_BROWSER_ENABLED is not set."}
        b = get_browser()
        st = b.refresh_status() if b.running else b.status
        return {"enabled": True, **st.as_dict()}

    @router.post("/start")
    def start(payload: dict | None = None, _: None = Depends(require_auth)) -> dict[str, Any]:
        _require_enabled()
        host = str((payload or {}).get("host") or "").strip() or None
        try:
            st = get_browser().start(host)
        except RemoteBrowserUnavailable as exc:
            raise HTTPException(503, str(exc)) from exc
        return st.as_dict()

    @router.post("/stop")
    def stop(_: None = Depends(require_auth)) -> dict[str, Any]:
        _require_enabled()
        return get_browser().stop().as_dict()

    @router.post("/navigate")
    def navigate(payload: dict, _: None = Depends(require_auth)) -> dict[str, Any]:
        _require_enabled()
        host = str((payload or {}).get("host") or "").strip()
        if not host or "." not in host:
            raise HTTPException(400, "A community host is required, e.g. altea.circle.so")
        return get_browser().navigate(host).as_dict()

    @router.get("/screenshot")
    def screenshot(_: None = Depends(require_auth)) -> Response:
        _require_enabled()
        b = get_browser()
        if not b.running:
            raise HTTPException(409, "The remote browser is not running.")
        return Response(content=b.screenshot(), media_type="image/png",
                        headers={"Cache-Control": "no-store"})

    @router.post("/input")
    def send_input(payload: dict, _: None = Depends(require_auth)) -> dict[str, Any]:
        """Relay a click/keystroke into the remote browser.

        This is how you drive Circle's own login form. Typed text goes straight
        to the browser; it is never logged or persisted here.
        """
        _require_enabled()
        b = get_browser()
        if not b.running:
            raise HTTPException(409, "The remote browser is not running.")
        kind = str((payload or {}).get("kind") or "")
        if kind == "click":
            try:
                b.click(int(payload["x"]), int(payload["y"]))
            except (KeyError, TypeError, ValueError) as exc:
                raise HTTPException(400, "click needs integer x and y.") from exc
        elif kind == "type":
            b.type_text(str(payload.get("text") or ""))
        elif kind == "press":
            key = str(payload.get("key") or "")
            if not key:
                raise HTTPException(400, "press needs a key.")
            b.press(key)
        else:
            raise HTTPException(400, "kind must be click, type, or press.")
        return {"ok": True}

    @router.post("/probe")
    def probe(payload: dict, _: None = Depends(require_auth)) -> dict[str, Any]:
        """Run the PoC and return its verdict."""
        _require_enabled()
        host = str((payload or {}).get("host") or "").strip()
        if not host or "." not in host:
            raise HTTPException(400, "A community host is required, e.g. altea.circle.so")
        b = get_browser()
        mode = str((payload or {}).get("mode") or "environment")
        report = (probe_authenticated(b, host) if mode == "authenticated"
                  else probe_environment(b, host))
        return report.as_dict()

    return router
