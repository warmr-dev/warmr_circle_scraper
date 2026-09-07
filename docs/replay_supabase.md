# Version B on Supabase (session replay experiment)

**What Version B is:** you export your Circle cookies from a browser where you're
logged in, the backend stores them **encrypted**, and it replays them
server-side to read your communities. It is built at your explicit request and
overrides the project's original "no cookie replay" rule.

**Measured result:** `challenged`. Circle's Cloudflare re-verifies a replayed
session in a fresh browser context — even from a residential IP — so this does
not currently yield a working ingestion path. Wiring it to Supabase makes it
*run* on your real database; it does not change that outcome. See
`docs/remote_browser_poc.md`.

---

## How it works

```
Export cookies (Chrome / EditThisCookie JSON)
        │
        ▼
POST /api/replay/store
  parse_cookies()      normalize to Playwright cookie shape
  encrypt()            AES-GCM with CIRCLE_CRED_KEY  ──► replay_sessions (ciphertext)
        │
        ▼
POST /api/replay/test
  load_cookies()       decrypt with CIRCLE_CRED_KEY
  replay_session()     load into a fresh Chromium, hit Circle
        │
        ▼
  result: ok | challenged | expired | error   ──► stored on the row
```

One row per host in `replay_sessions`. The cookie blob is always ciphertext;
the encryption key lives only in the backend environment, never in the DB.

## Wire it to Supabase

### 1. Point the app at Supabase Postgres

Set `CIRCLE_LEADS_DB` to your Supabase connection string (Project → Settings →
Database → Connection string → URI). Use the **session pooler** URI for a
long-running server:

```
CIRCLE_LEADS_DB=postgresql://postgres.<ref>:<password>@aws-0-<region>.pooler.supabase.com:5432/postgres
```

The app rewrites `postgresql://` to the psycopg-v3 driver automatically.

### 2. Create the table

Two options, pick one:

- **Automatic (default):** the app runs SQLAlchemy `create_all()` on first
  connect and creates `replay_sessions` (and every other table) for you. Nothing
  to do.
- **Explicit:** run [`deploy/supabase/replay_sessions.sql`](supabase/replay_sessions.sql)
  in the Supabase SQL editor. Idempotent (`IF NOT EXISTS`), so it's safe
  alongside the automatic path.

### 3. Set the required environment

```
REMOTE_BROWSER_ENABLED=true              # the remote-browser/replay surface is opt-in
CIRCLE_CRED_KEY=<a strong passphrase>    # encryption key; store refuses without it
```

### 4. Chromium in the image

Replay launches a real browser, which the default Dockerfile omits. To run it on
Railway, add to the Dockerfile:

```dockerfile
RUN pip install --no-cache-dir -e '.[web,browser,crypto]'
RUN playwright install --with-deps chromium
```

## Security

- **Cookies are stored encrypted (AES-GCM).** The store refuses to write without
  `CIRCLE_CRED_KEY`; plaintext is never persisted. Verified: the Postgres row is
  ciphertext.
- **The key is never in the database.** It lives only in the backend env. A DB
  dump without the key is useless.
- **Keep the table off the public API.** The app connects as the Postgres
  owner/service role (bypasses RLS), so it needs no policy — but the Supabase
  anon/publishable key must not be able to read this table. Enable RLS with no
  anon policy, or keep it out of the exposed schema:

  ```sql
  ALTER TABLE replay_sessions ENABLE ROW LEVEL SECURITY;
  -- add no policy for anon -> anon cannot read it
  ```

- **These are captured member sessions, not API tokens.** Rotate them (log out /
  back in on Circle) if they are exposed, and delete the row from the dashboard
  when done.

## The honest bottom line

Everything above makes Version B *run* on Supabase. It does not make it *work*:
the replay is challenged by Cloudflare regardless of where it runs. The reliable
path remains the local connector, where you clear the challenge yourself during
a normal login and the session stays on your machine.
