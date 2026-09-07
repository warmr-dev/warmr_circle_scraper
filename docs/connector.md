# Circle Connector — private-community reading

The Circle Connector lets you feed **private Circle communities you are a member
of** into the lead pipeline, without ever giving the application your Circle
password and without any community admin issuing a token.

You log into each community **yourself, in a real browser on your own computer**.
A small local process (the *connector*) reuses that browser session to read what
your account can already see, normalizes it, and uploads only the resulting text
to your Railway backend over HTTPS.

Public/free communities do **not** use the connector — they are read directly by
the built-in public reader with no login.

---

## Why it works this way (the Circle constraint)

Circle's official API cannot let a normal member authenticate and read the
private communities they belong to. Verified against Circle's live OpenAPI specs
(2026): every API token (Admin v1/v2, Headless Auth) is **created by a community
admin** and is **scoped to one community**; there is no member OAuth, no member
personal-access token, and **no "list my communities" endpoint**. The member JWT
even carries a single hard-coded `community_id` claim.

So member-level private access is done the only compliant way: your own
authenticated browser session, kept local. See the "Multi-community behavior"
note below for the consequence.

---

## Architecture

```
PUBLIC COMMUNITY
    public_reader  (no login)
        → HTTPS → RAILWAY → classifier → PostgreSQL → dashboard

PRIVATE COMMUNITY
    YOUR COMPUTER
      Playwright browser  ── you personally log into Circle
        │  (authenticated session stays LOCAL, in a Playwright profile)
        ▼
      local connector  ── reads only what your session can see, normalizes it
        │
        └── HTTPS (connector token) ─▶ RAILWAY
                                         ├── classifier
                                         ├── lead processing
                                         ├── PostgreSQL
                                         └── admin dashboard
```

**Railway hosts:** the dashboard, the API, PostgreSQL, the classifier, and lead
processing.

**Your local machine hosts:** Playwright, the Circle browser, your authenticated
Circle sessions, and the connector process. The connector is **intentionally NOT
a Railway process** — the browser session must stay on your machine.

---

## Complete workflow, from zero

### 1. Deploy the backend to Railway

Deploy this repo to Railway (it builds from the `Dockerfile`; the default command
starts the dashboard). Add a PostgreSQL database and point `CIRCLE_LEADS_DB` at it.

### 2. Configure Railway environment variables

| Variable | Purpose |
| --- | --- |
| `CIRCLE_LEADS_DB` | Postgres URL (use Railway's `${{ Postgres.DATABASE_URL }}` reference to avoid a malformed/empty-port URL) |
| `DASHBOARD_PASSWORD` | Dashboard login (≥ 8 chars) |
| `DASHBOARD_SECRET_KEY` | Session-signing key (Railway can generate one) |
| `DASHBOARD_HTTPS` | `true` in production |
| `OPENAI_API_KEY` | Classifier LLM escalation (optional but recommended) |
| `EXA_API_KEY` | Public-community discovery (optional) |

The connector needs **no** Railway secret of its own.

### 3. Open the admin dashboard

Visit your Railway URL, sign in with `DASHBOARD_PASSWORD`.

### 4. Open **Circle Connector**

Left sidebar → **Circle Connector**.

### 5. Generate a one-time pairing code

Click **Pair a local connector**. The dashboard shows a code (valid 15 minutes)
and the exact `circle-connector pair …` command to run.

### 6. Install the local connector (on your computer)

```bash
pip install 'circle-leads[browser]'
playwright install chromium
```

### 7. Pair the connector with Railway

```bash
circle-connector pair --backend https://<your-app>.up.railway.app --code <CODE>
```

This exchanges the code for a connector API token, saved to
`~/.circle-leads/connector.json` (permissions `0600`). It is uploaded nowhere.

### 8. Add a private Circle community host

In the dashboard's **Private communities** box, enter the host, e.g.
`altea.circle.so`, and click **Add community**. (You add each host explicitly —
see "Multi-community behavior".)

### 9. Log into the community — personally, in a browser

```bash
circle-connector login altea.circle.so
```

A Chromium window opens. **You** sign in to Circle. The connector never sees or
handles your password; it waits until it detects a valid session, then saves it
to a local Playwright profile.

### 10. Verify the authenticated session

```bash
circle-connector status
```

Shows the backend URL, that the connector is paired, and whether the backend is
reachable.

### 11. Sync once

```bash
circle-connector sync altea.circle.so
```

The connector checks the local session, enumerates the spaces your account can
access, reads their posts locally, normalizes them, and uploads the records.

### 12. Verify in the dashboard

The **Circle Connector** tab shows the community as **Connected**, the member you
are signed in as, and `readable / total` space counts. New leads appear on the
**Leads** tab; the upload is logged on **Activity**.

### 13. Run continuous scraping

```bash
circle-connector run altea.circle.so another.circle.so --interval 600
```

Loops: sends a heartbeat (so the dashboard shows the connector **online**) and
re-syncs each community every `--interval` seconds. `Ctrl-C` to stop.

---

## Security

- **Your Circle password never enters the application.** You type it into
  Circle's own login page, in your own browser.
- **Circle cookies / session tokens never go to Railway.** They live only in the
  local Playwright profile.
- **Browser profiles stay on the local machine** (default `~/.circle-leads/browser-profile/<host>/`).
- **The connector API token is only for authenticating the local connector to
  your Railway backend.** It is **not** a Circle credential and grants no Circle
  access.
- The connector token is stored locally at `~/.circle-leads/connector.json` with
  restrictive permissions (`0600`); only its SHA-256 hash is stored on the
  backend, never the token itself.
- **Browser profiles and the connector token must not be committed to Git.** The
  repo's `.gitignore` covers `.circle-leads/`, `circle_profiles/`,
  `browser-profile/`, and `connector.json`. The default state directory is under
  your home directory, outside the repo, in any case.
- The connector uploads **only normalized content records and non-sensitive
  connection state** (host, auth state, space counts).

---

## Multi-community behavior

Circle has **no universal "communities this member belongs to" endpoint** for our
use case. Consequently:

- You **explicitly add each community host** you want to monitor.
- Each community gets its **own local browser profile/session**.
- **You log in personally for each community** the first time (and again if its
  session expires).
- **Spaces within an authenticated community are discovered automatically** — you
  do not list spaces by hand.
- Scraping uses **only content available to that authenticated browser session**.

> The connector can access content exposed to the authenticated member browser
> session. Spaces and content that the account cannot access are not scraped.

---

## Session expiration

Circle sessions expire. The flow is:

```
CONNECTED
  → session expires
  → connector detects it on the next sync
  → reports SESSION_EXPIRED
  → dashboard shows "Authentication required"
  → you run:  circle-connector login <host>
  → sign in again in the browser
  → connector resumes on the next sync
```

The connector distinguishes these states separately and reports each to the
dashboard: `not_connected`, `authentication_required`, `authenticating`,
`connected`, `session_expired`, `access_denied`, `error`. "Logged in" is not the
same as "can access the community" is not the same as "spaces are readable" —
each is detected on its own.

---

## Command reference

| Command | What it does |
| --- | --- |
| `circle-connector pair --backend <url> --code <CODE>` | Pair with Railway using a one-time code |
| `circle-connector login <host>` | Open a browser so you sign into a community yourself |
| `circle-connector sync <host> [--max-pages N]` | Read + upload one community once |
| `circle-connector run <host> [<host> …] [--interval S] [--max-pages N]` | Heartbeat + sync on a loop |
| `circle-connector status` | Show local pairing + backend reachability |
