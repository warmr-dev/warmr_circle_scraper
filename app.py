"""ASGI entrypoint for hosted deploys (Vercel, uvicorn).

Vercel looks for a top-level FastAPI instance named ``app`` in ``app.py`` /
``main.py``. ``main.py`` is the CLI, so the dashboard is exported here.

The real FastAPI app is built on first request so a Vercel build can import
this module before runtime env vars (``DASHBOARD_PASSWORD``,
``CIRCLE_LEADS_DB``) are used. Those are still required to serve traffic —
the same as ``circle-leads dashboard``.
"""

from __future__ import annotations

from typing import Any


class _LazyApp:
    """ASGI wrapper around ``create_app()``.

    Vercel's FastAPI builder AST-scans for a top-level ``app`` assignment and
    the runtime probes ``app.asgi`` during import. Neither step should connect
    to Postgres or require secrets.
    """

    __slots__ = ("_app",)
    asgi = None  # Vercel detect_app_type; keep import side-effect free

    def __init__(self) -> None:
        self._app = None

    def _ensure(self) -> Any:
        if self._app is None:
            from circle_leads.web.app import create_app

            self._app = create_app()
        return self._app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        await self._ensure()(scope, receive, send)


app = _LazyApp()
