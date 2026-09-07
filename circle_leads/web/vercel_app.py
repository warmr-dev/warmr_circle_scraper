"""Vercel serverless entrypoint (`tool.vercel.entrypoint` in pyproject.toml).

Read `deploy/DEPLOY.md` before using this. This app was built as a persistent
FastAPI server with in-process background jobs (harvest/search), an
in-memory session/job registry, and (by default) a local SQLite file -- none
of which survive across Vercel's stateless, per-invocation serverless
functions. This module only satisfies Vercel's "export a top-level `app`"
requirement so the build doesn't fail immediately; it does not make the
dashboard's background-job or session behavior work correctly on Vercel.

To even get this far without a *different* crash at import time, both of
these must be set as Vercel Environment Variables (Build AND Runtime):

  DASHBOARD_PASSWORD   at least 8 characters -- create_app() raises if unset.
  CIRCLE_LEADS_DB       an external Postgres URL (e.g. Supabase). Without it,
                        Database(None) tries to create a local "data/" dir,
                        which fails on Vercel's read-only filesystem.

Even then, expect: sessions/job status resetting on every cold start (or
differing between concurrent instances), and long-running jobs (harvest,
search, feed reads) getting killed when the function times out. For real
use, deploy to Render/Railway as described in deploy/DEPLOY.md instead.
"""

from circle_leads.web.app import create_app

app = create_app()
