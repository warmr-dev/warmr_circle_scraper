# Deploying Warmr Circle

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

**Service 2 — worker (does ALL the scanning + harvest):**

The always-on worker drains the **scan-job queue** (private-community scans the
dashboard enqueues) AND runs the scheduled public harvest. The dashboard/UI
never scans inside a request -- it just writes a job; this warm, pooled process
does the work fast, with no serverless timeout:
```
Start: circle-leads --db "$CIRCLE_LEADS_DB" worker
Env:   CIRCLE_LEADS_DB, EXA_API_KEY, OPENAI_API_KEY, CIRCLE_CRED_KEY
```
`worker` loops forever: it claims and runs queued scan/scan_all/harvest jobs
immediately, and when the queue is empty it checks the dashboard schedule and
harvests when due. Deploy this on Railway (a persistent process); point the
Vercel dashboard at the same `CIRCLE_LEADS_DB`, and pressing "Scan now" there
returns instantly while the worker does the reading.

(The older `harvest --loop --poll-seconds 60` still works for harvest-only, but
`worker` is preferred -- it also drains the scan queue.)
`--loop` runs forever: every `--poll-seconds` it checks `is_harvest_due` (the
interval you picked in the dashboard Config tab, down to every 5 min) and only
harvests when due. So the worker is a *fixed* frequent poller; the *actual*
cadence is controlled from the dashboard with no redeploy. It needs no HTTP
port and no external pinger.

Alternatives:
- Platform cron: run one-shot `circle-leads --db "$CIRCLE_LEADS_DB" harvest
  --scheduled` on a frequent cron (it self-skips when not due).
- No separate worker at all: run only the web service and hit its
  `/api/tick?token=...` from an external pinger (see the Render section).

### Why the dashboard can't go on Vercel
The dashboard is a **persistent FastAPI server** with in-process background
jobs, a pooled HTTP session, and a long-running harvest -- none of which fit
Vercel's serverless model (functions time out in seconds and hold no state).
"Worker on Railway, dashboard on Vercel" is therefore not possible as-is: put
**both** on Railway (two services above). The only way to involve Vercel would
be to split the dashboard into a static SPA (Vercel) talking to the FastAPI API
on Railway over CORS -- a real refactor, not a config change.

### If you try Vercel anyway (build error: "does not define a top-level app")
`main.py` at the repo root is the **CLI** entrypoint, not a FastAPI app, so
Vercel's Python/FastAPI preset can't find an `app` instance there and the build
fails immediately. A top-level `app.py` now exports a **lazy** ASGI app (see
`app.py`; `tool.vercel.entrypoint = "app:app"` in `pyproject.toml`), so the
build and cold-import succeed without env vars or a database. That fixes the
*build* error, but does not make the app run correctly on Vercel:

- To serve traffic, `DASHBOARD_PASSWORD` (8+ chars) must be set as a Vercel
  **Environment Variable** (Runtime), or the first request raises.
- Without `CIRCLE_LEADS_DB` pointing at an external Postgres (e.g. Supabase),
  `Database(None)` tries to create a local `data/` dir, which fails on Vercel's
  read-only filesystem.
- Sessions (`SessionManager`) and the background job registry (`JobRegistry`)
  are in-memory. They don't survive cold starts or get shared across concurrent
  instances, and long jobs (search, harvest, feed reads) get killed when the
  function's window ends.
- Config edits from the dashboard are persisted in the **database**, not the
  packaged `requirements.yaml` (which is read-only on Vercel: `/var/task` →
  `Errno 30`). This is handled automatically; just make sure `CIRCLE_LEADS_DB`
  is set.
- The Playwright-based features (the local connector's ingest, the remote-
  browser PoC, the replay experiment) **cannot run on Vercel at all** — no
  browser, no persistent process. Those belong on the local connector /
  Railway, never on serverless.

In short: this unblocks the build error, but Vercel is still the wrong platform
to actually run the dashboard on. Use Render/Railway above for anything beyond a
quick experiment.

## The database

- **Use Postgres.** Point `CIRCLE_LEADS_DB` at its connection URL
  (`postgresql://user:pass@host:5432/dbname`). The storage layer is SQLAlchemy,
  so only the URL changes — no code changes.
- **Supabase:** prefer the **transaction-mode** pooler (port **6543**, host
  `*.pooler.supabase.com`). Session mode (the same host on port 5432) allows
  only ~15 clients and fails with `EMAXCONNSESSION` / "max clients reached"
  as soon as SQLAlchemy's default pool plus a leftover deploy fills it. If
  you paste the session-mode URL, the app rewrites it to port 6543 and keeps
  a pool of 3 connections.
- **SQLite does not survive** on these platforms (ephemeral disks), so a local
  `data/circle_leads.db` would be wiped on every redeploy. That's why the cloud
  needs Postgres.
- The Dockerfile installs `psycopg[binary]` so the Postgres URL works out of the box.

### Serverless (Vercel) + Supabase: avoid pooler exhaustion

On Vercel each request may run in a fresh function instance. With Supabase's
**session-mode pooler (port 5432)** each instance holds a connection, and the
small pool (`pool_size: 15`) is exhausted fast:

```
FATAL: (EMAXCONNSESSION) max clients reached in session mode
```

To avoid it:

1. **Use the transaction pooler (port 6543), not session mode (5432).** In
   Supabase: Settings → Database → Connection string → **Transaction**. The URI
   ends in `:6543/postgres` (it may add `?pgbouncer=true`). Set it as
   `CIRCLE_LEADS_DB`.
2. **Set `SKIP_DB_INIT=true`.** Otherwise every cold start opens a connection to
   run `create_all()`. Create the tables once (below), then skip it.
3. `DB_NULLPOOL=true` is optional — the code already uses `NullPool` when it
   detects Vercel/Lambda, so a connection is closed after each request instead
   of being held. This var just forces it anywhere.

**Create the tables once** (locally, pointed at Supabase — do this before setting
`SKIP_DB_INIT`):

```bash
CIRCLE_LEADS_DB='postgresql://postgres.<ref>:<pw>@aws-0-<region>.pooler.supabase.com:6543/postgres' \
  python -c "from circle_leads.storage.database import Database; \
Database('$CIRCLE_LEADS_DB'); print('tables created')"
```

None of this makes the browser features work on Vercel — the dashboard, API,
leads and classifier work once the DB is reachable; the Playwright pieces still
need the local connector.

## What is NOT deployed to the cloud

- **The browser feed reader (`read-feed` / `read-all`)** for *private* communities
  needs a real logged-in browser (Playwright + your Chrome profile). That stays
  **local, on your machine** — a server can't hold your Circle login safely, and
  the base image omits Chromium. The cloud harvest only reads *public* spaces,
  which need no login.

## Secrets

Never commit `.env`. In the cloud, set every key as a platform env var:
`DASHBOARD_PASSWORD`, `DASHBOARD_SECRET_KEY`, `EXA_API_KEY`, `OPENAI_API_KEY`,
`CIRCLE_LEADS_DB`, `SUPABASE_ANON_KEY`, `VINI_API_SECRET`.
The `.gitignore` already excludes `.env`.

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
