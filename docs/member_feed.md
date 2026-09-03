# Reading a feed you're a member of (Python, automatic)

For communities you belong to, `read-feed` reads a space's posts and runs them
through the lead classifier — no copy/paste.

## The safe design: your browser holds the login, not the code

It uses a **Chromium browser with a persistent profile** on your machine. You
log into Circle once, in that window. The session then lives in the browser's
own cookie store, exactly like your normal browser.

**Python never sees, copies, stores, or transmits your session token.** The
browser holds it and sends it automatically. There is no cookie to paste, no
token in an env var, nothing sensitive in the code or logs. It's read-only:
nothing is joined, posted, or changed.

> Do not paste cookies or tokens into a terminal or chat. A Circle session
> cookie is a full login to your account. This tool is built specifically so
> you never have to handle one.

## Setup

```bash
pip install 'circle-leads[browser]'
playwright install chromium
```

## Step 1 — Sign in once

```bash
circle-leads read-feed startupandangels.circle.so --login
```

A browser window opens. Log into Circle there. It detects the login, saves the
session to the profile, and closes. Repeat only when the session expires.

## Step 2 — Find the space id

The feed endpoint uses a numeric space id (not the `/c/<slug>` in the URL):

1. Open the space in your browser, DevTools → Network → reload.
2. Find a request like `internal_api/spaces/1595123/posts` — the number is the id.

Startup&Angels: **Job Posts = `1595123`**. Other spaces (Opportunities & Collabs,
Community Posts, Ask an Expert) — look each up the same way.

## Step 3 — Read

```bash
circle-leads read-feed startupandangels.circle.so --space-id 1595123 --community startupandangels
```

Add more `--space-id` flags for several spaces; `--use-llm` to escalate
ambiguous posts. Leads land in the dashboard's Leads tab and in `search`.

## Notes

- It reads the truncated post body (~255 chars) plus the title — enough to
  classify hiring intent. Open the permalink for the full text before replying.
- Re-runs de-duplicate, so you only classify what's new.
- These are Circle's internal endpoints (not a documented API), read at a
  browser's pace as a member reading their own feed. If Circle changes them or
  the login lapses, the command fails cleanly — re-run with `--login`.
- `--show-browser` runs the browser visibly, for debugging.

## Automating it on your machine (every community, on a schedule)

Once you've signed into each community once, you can read them all
automatically — no logins, no tokens, running locally on your account.

### 1. List your communities and spaces

Copy the template and fill in the communities you belong to:

```bash
cp circle_leads/config/communities/member_feeds.yaml.template \
   circle_leads/config/communities/member_feeds.yaml
```

```yaml
feeds:
  - host: startupandangels.circle.so
    slug: startupandangels
    spaces:
      - id: 1595123
        name: "Job Posts"
  - host: ai-community-215177.circle.so
    slug: ai-community
    spaces:
      - id: 234567
        name: "General"
```

(This file holds no secrets — just hosts and space ids — and is gitignored.)

### 2. Sign into each, once

```bash
circle-leads read-feed startupandangels.circle.so --login
circle-leads read-feed ai-community-215177.circle.so --login
```

Each opens a window; you sign in (SSO included) and it saves that community's
session to its own browser profile under `~/.circle-leads/`.

### 3. Check status any time

```bash
circle-leads sessions
```

```
STATUS         SPACES  COMMUNITY
------------------------------------------------------------
signed in           1  startupandangels.circle.so
sign in needed      1  ai-community-215177.circle.so
```

### 4. Read them all — manually or on a schedule

```bash
circle-leads read-all --use-llm       # every signed-in community, once
```

To run it automatically on your machine, add a cron line (see
`deploy/read-all.cron.example`):

```cron
0 */4 * * *  cd /path/to/circle-scraper && ./.venv/bin/circle-leads read-all --use-llm
```

That's the full local loop: **you sign into each community once; your machine
reads their feeds every few hours and files the leads** — no login automation,
no stored tokens, nothing leaving your control. A community whose session has
expired is simply skipped until you re-run its `--login`.
