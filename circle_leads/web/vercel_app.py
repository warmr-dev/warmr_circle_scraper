"""Backward-compat Vercel entrypoint.

An earlier Vercel setup pointed at ``circle_leads.web.vercel_app:app``. The
canonical entrypoint is now the top-level ``app.py`` (a lazy ASGI app that
imports without env vars or a database, so Vercel's build/cold-import can't
crash). This module re-exports it so an older ``tool.vercel.entrypoint`` or
Vercel project setting keeps working.

Prefer ``app:app``. See ``app.py`` and ``deploy/DEPLOY.md``.
"""

from app import app  # noqa: F401 - re-exported for backward compatibility
