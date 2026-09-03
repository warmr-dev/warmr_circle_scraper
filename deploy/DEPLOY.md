# Deploying Circle Leads

## Short answer

- **Do NOT use Vercel.** This is a Python app with a background worker, a browser
  scraper, and a SQL database — none of which fit Vercel's serverless model
  (no persistent disk, no long-running jobs, no browser). Vercel is for static
  sites / JS serverless functions; there is no separate frontend to put there.
- **Deploy the whole repo to Render / Railway / Out Plane** as **two services
  that run the same code**, plus one Postgres database.

```
Render / Railway / Out Plane
├── web service     →  circle-leads dashboard   (the UI you open)
├── worker/cron     →  circle-leads harvest      (finds leads on a schedule)
└── Postgres        →  shared by both (SQLite does NOT persist in the cloud)
```

## Which directory?

**The repository root, for both services.** You do not split directories — both
run the same package from the root; they differ only by the start command. The
`deploy/Dockerfile` (at the root of the build) builds one image used by both.

## Render (easiest — one blueprint file)

`render.yaml` at the repo root declares everything:

1. In Render: **New → Blueprint**, point it at this GitHub repo.
2. It creates: the **web** service, the **harvest cron**, and a **Postgres** DB,
   wiring `CIRCLE_LEADS_DB` to the DB automatically.
3. After the first deploy, set the secret env vars (marked `sync: false`) in the
   Render dashboard:
   - `DASHBOARD_PASSWORD` — a strong password (min 8 chars)
   - `EXA_API_KEY` — for good community discovery
   - `OPENAI_API_KEY` — for AI classification of ambiguous posts (optional)

The cron runs `harvest` at 8am and 8pm UTC. Leads appear in the web dashboard.

## Railway / Out Plane (manual, same idea)

Create **two services from this repo** sharing one Postgres. Set
`CIRCLE_LEADS_DB` to the Postgres connection URL on both.

**Service 1 — web (the dashboard):**
```
Start: circle-leads --db "$CIRCLE_LEADS_DB" dashboard --host 0.0.0.0 --port $PORT
Env:   CIRCLE_LEADS_DB, DASHBOARD_PASSWORD, DASHBOARD_SECRET_KEY,
       DASHBOARD_HTTPS=true, EXA_API_KEY, OPENAI_API_KEY
```

**Service 2 — worker (the harvest):**
- If the platform has scheduled jobs / cron, run a one-shot on a schedule:
  ```
  Start: circle-leads --db "$CIRCLE_LEADS_DB" harvest --use-llm --verbose-log
  Env:   CIRCLE_LEADS_DB, EXA_API_KEY, OPENAI_API_KEY
  ```
- If it only supports always-on services, loop it in the container instead, e.g.
  a shell that runs `harvest` then `sleep 43200` (12h). Prefer real cron.

## The database

- **Use Postgres.** Point `CIRCLE_LEADS_DB` at its connection URL
  (`postgresql://user:pass@host:5432/dbname`). The storage layer is SQLAlchemy,
  so only the URL changes — no code changes.
- **SQLite does not survive** on these platforms (ephemeral disks), so a local
  `data/circle_leads.db` would be wiped on every redeploy. That's why the cloud
  needs Postgres.
- The Dockerfile installs `psycopg[binary]` so the Postgres URL works out of the box.

## What is NOT deployed to the cloud

- **The browser feed reader (`read-feed` / `read-all`)** for *private* communities
  needs a real logged-in browser (Playwright + your Chrome profile). That stays
  **local, on your machine** — a server can't hold your Circle login safely, and
  the base image omits Chromium. The cloud harvest only reads *public* spaces,
  which need no login.

## Secrets

Never commit `.env`. In the cloud, set every key as a platform env var:
`DASHBOARD_PASSWORD`, `DASHBOARD_SECRET_KEY`, `EXA_API_KEY`, `OPENAI_API_KEY`,
`CIRCLE_LEADS_DB`. The `.gitignore` already excludes `.env`.

## Out Plane worker (with dashboard-controlled schedule)

The harvest schedule is stored in the DB and editable from the dashboard's
Config tab (Off / hourly / 6h / 12h / twice daily / daily / weekly). The worker
runs `harvest --scheduled`, which checks that interval and only harvests when
it's due -- so you run the worker on a *fixed* frequent cron and control the
*actual* harvest cadence from the dashboard without redeploying.

Get started (from https://docs.outplane.com/cli/agents):
```bash
curl -fsSL https://outplane.com/install.sh | sh
outplane login                 # approve in the console
outplane app create --repo Rasreal/circle-scraper --branch main
```

Worker command (set as the app's start command / a scheduled job):
```
circle-leads --db "$CIRCLE_LEADS_DB" harvest --scheduled --use-llm --verbose-log
```
Run it hourly (cron `0 * * * *`); it harvests only when the dashboard schedule
says it's due. Env: CIRCLE_LEADS_DB (Postgres), EXA_API_KEY, OPENAI_API_KEY.
