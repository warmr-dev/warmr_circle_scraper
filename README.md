# Warmr Circle

Finds posts where **someone wants to hire** across Circle.so communities — your
own private ones and public ones — and filters out people who are **looking for
work**.

```
"We are looking for a backend developer."         -> LEAD
"I am looking for a job as a software engineer."  -> NOT_LEAD
```

It reads your **private paid communities** with your own session, reads **public**
communities with no login, classifies every post/comment as hire-vs-seek, scores
and de-duplicates the leads, and surfaces them in a dashboard. The heavy work runs
on an always-on worker; the dashboard stays instant.

---

## How it reads Circle (the honest version)

This project was built on a specific, tested finding — not on assumptions:

**Circle has no member API.** Verified against Circle's live OpenAPI specs: no
member OAuth, no member personal-access token, and no "list my communities"
endpoint. Every official API token is created by a community **admin** and scoped
to one community. So an official-API integration would need each community's
operator to hand you a token.
→ [`docs/circle_auth_investigation.md`](docs/circle_auth_investigation.md)

**But Circle's `/internal_api/*` JSON works with your own session cookie.** The
endpoints Circle's own web app calls (`/internal_api/spaces`,
`/spaces/{id}/posts`, `/posts/{id}/comments`, `/comments/{id}/comments`) return
real JSON to an ordinary HTTPS client carrying your member session cookie —
**and they are not behind the Cloudflare challenge** that gates the HTML pages.
So the app reads your communities over plain HTTP, with **no browser**, from
anywhere.
→ [`docs/circle_auth_investigation.md`](docs/circle_auth_investigation.md), and
the endpoint map in the project memory.

Two cookies are required per community (`_circle_session` +
`user_session_identifier`), and Circle scopes a session **per subdomain**, so you
provide one cookie set per community. Approaches that were built, measured, and
**do not work** — a server-hosted browser and cookie-replay-in-a-fresh-browser
(both hit Cloudflare), and an automated password login (Turnstile on the login
form) — are documented in [`docs/remote_browser_poc.md`](docs/remote_browser_poc.md)
and kept behind an off-by-default flag as evidence.

### What it reads, and what it can't

| | |
|---|---|
| Your **private** communities (posts, comments, replies), via your session cookie | ✅ |
| Full post/comment text (flattened from Circle's rich-text body, not truncated) | ✅ |
| **Public** communities (posts), no login | ✅ |
| Discover new communities by web search; re-scan existing ones | ✅ |
| Classify hire-vs-seek, extract role/skills/budget, score, de-duplicate, export | ✅ |
| **Chat / DMs** | ❌ separate real-time service with its own token; not scraped |
| Defeat a Cloudflare / CAPTCHA challenge | ❌ never — it stops and reports |

---

## Architecture

Three moving parts. The dashboard never does heavy work in a request — it writes
a job; the worker does the scanning.

```
                    ┌─────────────────────────┐
                    │        Supabase         │  (Postgres)
                    │  communities · posts    │
                    │  leads · scan_jobs      │  ← the queue
                    │  scan sessions · config │
                    └────────▲───────▲────────┘
             enqueue job     │       │   claim + run job
          ┌──────────────────┘       └──────────────────┐
          │                                              │
 ┌────────┴─────────┐                          ┌─────────┴──────────┐
 │  Dashboard (UI)  │                          │  Worker (always-on)│
 │  Vercel/Render   │                          │  Railway/Render    │
 │                  │                          │                    │
 │ press "Scan now" │  → writes scan_jobs row →│ polls the queue,   │
 │ returns in ~40ms │                          │ scans /internal_api│
 │ shows leads      │                          │ VIP first, then    │
 │                  │                          │ public harvest     │
 └──────────────────┘                          └─────────┬──────────┘
                                                          │  your session cookie
                                                          ▼
                                                      Circle.so
```

- **Dashboard** — the web app (`circle-leads dashboard`). Login, communities,
  leads, config. Pressing *Scan now* / *Scan all* just **enqueues** a job and
  returns instantly (~40 ms); it never scans inside the request. Runs fine on
  serverless (Vercel) because it does no long work.
- **Worker** — `circle-leads worker`, an always-on process. It drains the
  `scan_jobs` queue (private-community scans, VIP-first) and runs the scheduled
  **public harvest** (web-search discovery + reading). Warm, pooled, no timeout —
  this is where all the actual reading happens.
- **Supabase** — one Postgres database, shared by both. Holds communities,
  posts, leads, the job queue, encrypted session cookies, and settings.

Why the split: a serverless function times out (~60 s) and kills background
threads, so scanning many spaces inside a Vercel request fails. Enqueue-and-let-
the-worker-run makes the UI instant and the scanning unbounded.

---

## Data flow

```
discover (web search) ─┐
                       ├─→ read spaces → posts + comments + replies
your private cookies ──┘         │
                                 ▼
                    normalize (flatten rich text, redact PII)
                                 │
                                 ▼
             classify: rules → LLM escalation (ambiguous only)
                                 │
                                 ▼
            hire? → extract role/skills/budget → score → de-dup
                                 │
                                 ▼
                     store lead → dashboard → export (Vini)
```

**Classification** is the hard part: hiring and job-seeking use nearly identical
words. The decisive test is *who is the object of the search* — a person to hire
(LEAD) or employment for oneself (NOT_LEAD). Layer 1 is deterministic weighted
rules; only genuinely ambiguous posts escalate to an LLM (`--use-llm` /
`OPENAI_API_KEY`). Every lead keeps an exact evidence quote and which layer
decided it.

---

## Quick start (local)

Requires Python 3.11+.

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e '.[web]'            # dashboard + Postgres driver
# optional: '.[llm]' for LLM classification, '.[crypto]' to encrypt stored cookies
```

```bash
export DASHBOARD_PASSWORD='a-password-8-chars-min'
export CIRCLE_LEADS_DB='postgresql://…:6543/postgres'   # Supabase; omit for local SQLite

circle-leads dashboard          # the UI at http://127.0.0.1:8000
circle-leads worker             # in another terminal: drains the queue + harvests
```

Then in the dashboard → **Circle Connector** tab:

1. **Add a community** (e.g. `mycommunity.circle.so`), set its priority (VIP first).
2. **Add its session cookie** — log into that community in your browser, export
   its cookies, and paste the JSON (or the two values). Needs `_circle_session`
   and `user_session_identifier`. Stored encrypted if `CIRCLE_CRED_KEY` is set,
   else plaintext.
3. **Scan now** — the worker reads it; leads appear on the **Leads** tab.

---

## Deploy (dashboard + worker + Supabase)

Do **not** run the scanning on serverless — that's what the worker is for.

**Supabase** — create the tables once, then point both services at it:
```
CIRCLE_LEADS_DB = postgresql://postgres.<ref>:<pw>@aws-0-<region>.pooler.supabase.com:6543/postgres
```
Use the **transaction pooler (port 6543)** — session mode (5432) exhausts the
connection cap and hangs. The code auto-rewrites 5432→6543 for Supabase URLs, but
set 6543 explicitly. See [`deploy/DEPLOY.md`](deploy/DEPLOY.md).

**Dashboard** (Vercel/Render):
```
Start: circle-leads dashboard --host 0.0.0.0 --port $PORT
Env:   CIRCLE_LEADS_DB, DASHBOARD_PASSWORD, DASHBOARD_HTTPS=true, CIRCLE_CRED_KEY,
       SKIP_DB_INIT=true   # after tables exist
```
On Vercel the entrypoint is the lazy `app.py` (`tool.vercel.entrypoint`), which
imports without a DB so the build can't crash. `/api/health` reports DB status.

**Worker** (Railway/Render — an always-on process):
```
Start: circle-leads worker
Env:   CIRCLE_LEADS_DB, EXA_API_KEY, OPENAI_API_KEY, CIRCLE_CRED_KEY
```

Full guide, including the Vercel/Supabase pooler gotchas and the read-only-config
fix: [`deploy/DEPLOY.md`](deploy/DEPLOY.md).

---

## Scheduling

Set the harvest cadence in the dashboard's **Config** tab (stored in the DB). The
**worker** checks it and harvests when due — no external cron needed. If you run
the dashboard without a worker, an external pinger can hit
`GET /api/tick?token=<TICK_TOKEN>`, which enqueues a harvest job for the worker.

---

## Repository layout

```
circle_leads/
├── scanning.py          scan_cookie_host, cookie_hosts_vip_first (shared by app + worker)
├── harvest.py           public discovery (web search) + read + classify
├── pipeline.py          orchestration
├── scraper/
│   ├── member_api_reader.py   /internal_api reader: spaces, posts, comments, replies
│   ├── public_reader.py       public communities (no login)
│   └── normalize.py           rich-text flattening, PII redaction
├── discovery/           web search (Exa/Brave/DDG), validate, persist communities
├── classifier/          rules → LLM escalation, extraction
├── scoring/             lead scoring + priority
├── triage/              per-post classify/score/store loop
├── storage/
│   ├── models.py        the Supabase schema (12 tables)
│   ├── job_queue.py     the durable scan-job queue (enqueue, atomic claim, complete)
│   ├── database.py      engine (Supabase pooler tuning, shared_session batching)
│   └── replay_store.py  encrypted-at-rest session cookies, per community
├── web/
│   ├── app.py           the dashboard API (enqueues jobs; never scans inline)
│   └── static/index.html the dashboard UI
├── cli/main.py          circle-leads … (dashboard, worker, harvest, run, export, …)
└── remote_browser/      the measured-not-viable PoCs (server browser, cookie replay), off by default
```

**Supabase tables:** `communities`, `spaces`, `authors`, `posts`, `leads`,
`activity_log`, `settings`, `scrape_runs`, `circle_connections`,
`replay_sessions` (encrypted cookies), `scan_jobs` (the queue), `connectors`.

---

## Security

- **Session cookies are encrypted at rest** (AES-GCM) when `CIRCLE_CRED_KEY` is
  set; stored plaintext otherwise (your choice). Never logged, never returned to
  the frontend. One cookie set per community; delete/clear from the dashboard.
- **The `replay_sessions` table holds live member sessions.** Keep it off the
  Supabase anon/public API (enable RLS with no anon policy). The app connects as
  the Postgres owner, which bypasses RLS.
- **No Circle password is ever handled by the app** — you log into Circle
  yourself and hand over only the resulting session cookie.
- **Challenges are a stop signal, never bypassed.** Nothing here solves a CAPTCHA,
  spoofs a fingerprint, or evades Cloudflare; a challenge is reported and the run
  stops.
- **Rotate a cookie if it's exposed** (log out/in on that community). Cookies
  expire; re-paste when a scan reports the session expired.

---

## Tests

```bash
pip install -e '.[dev]' && pytest        # 548 tests, offline (no network, no browser)
```

---

## Further reading

- [`docs/circle_auth_investigation.md`](docs/circle_auth_investigation.md) — why there is no member API, and how `/internal_api` was verified.
- [`docs/remote_browser_poc.md`](docs/remote_browser_poc.md) — the server-browser and cookie-replay approaches that were measured and rejected.
- [`docs/replay_supabase.md`](docs/replay_supabase.md) — encrypted session storage on Supabase.
- [`deploy/DEPLOY.md`](deploy/DEPLOY.md) — dashboard + worker + Supabase, and the serverless gotchas.
- [`docs/connector.md`](docs/connector.md) — the optional local connector (a legacy path; the cloud cookie flow supersedes it).
- [`extension/README.md`](extension/README.md) — browser extension: one click to send a community's session cookie to the dashboard's Join Queue, instead of copy-pasting from DevTools.
