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

## Render (free tier)

Render's free tier runs **web services** for free (they sleep after 15 min idle,
wake on the next request). Cron jobs and background workers aren't a good free
fit, and Render's free Postgres **expires after 30 days** — so use **your
Supabase Postgres** and schedule the harvest with a free external pinger.

### 1. Deploy the web service

`render.yaml` (repo root) deploys one free web service:
1. Render: **New → Blueprint**, point at this repo.
2. After the first deploy, set these in the Render dashboard (Environment):
   - `CIRCLE_LEADS_DB` — your **Supabase** Postgres URL (`postgresql://...`)
   - `DASHBOARD_PASSWORD` — a strong password
   - `EXA_API_KEY`, `OPENAI_API_KEY`
   - `TICK_TOKEN` — any secret string (used by the pinger below)

### 2. Set the harvest schedule (in the app)

Open the deployed dashboard → **Config tab** → pick a schedule (hourly / daily /
etc.). It's stored in the DB; no redeploy to change it.

### 3. Schedule the harvest with a free pinger

The free web service has no cron, so an external pinger triggers the harvest.
Create a free job at **cron-job.org** (or any uptime pinger) that GETs, hourly:
```
https://<your-app>.onrender.com/api/tick?token=<TICK_TOKEN>
```
`/api/tick` runs a harvest **only when your dashboard schedule says it's due**,
in the background. It also keeps the free web service awake. So: the pinger runs
hourly, but the actual harvest cadence is whatever you set in the dashboard.

That's the whole free setup: one Render web service + Supabase + a free hourly
pinger.

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
