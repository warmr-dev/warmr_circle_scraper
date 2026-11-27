# Supabase egress

The database is small (about 200 MB). The free-plan quota is spent by reading
the same rows again: the watcher, the worker and the maintenance units on the
droplet, and the dashboard on Vercel, all talk to Postgres through the
Supavisor transaction pooler (port 6543). `pg_stat_statements` on project
`qxpewmsujqtbwhpddlti` was last reset on 2026-09-02. The numbers below were
measured on 2026-11-27.

Supabase's Fair Use Policy starts applying on 2026-12-27. The aim is a month
that projects under 5 GB, with 5.5 GB as the hard ceiling.

## What was expensive

Payload sizes are `pg_column_size` averages. With prepared statements off,
every execution also sends a RowDescription (column names and types). That
header is small on a one-column read and large on a 40-column communities row,
which is why a "one row" lookup dominated the bill.

Today's watch list, from `watch_state` (median gap between `last_checked_at`
and `next_check_at`):

| Who | Rows | Interval | Checks/day |
|---|---:|---:|---:|
| Public, fast tier | 34 | 120 s | 24,480 |
| Joined, no stored session, fast | 16 | 120 s | 11,520 |
| Stored session, fast | 56 | ~120 s | 40,320 |
| Public, slow tier | 128 | ~15 min | 12,400 |
| Off | 159 | 1 day | 160 |

About 89,000 checks a day if that schedule holds. The 85-day statement stats
average closer to 20,000 finishes a day, because the list was smaller for most
of that window. December will be billed at the current schedule, not the
average.

## What the code does about it

- **Finish a check without the wide columns.** `_finish` used to
  `session.get` the whole `watch_state` row (the `recent_ids` watermark is
  most of those bytes, and the RowDescription named every column). It now
  re-reads only the columns the decision needs — mode, tier, counters,
  timestamps — so a join or a tier change made after the batch was loaded is
  still what gets scheduled. `recent_ids` is selected only when this check
  has ids to append. Statuses, error counters and the backoff formula are
  unchanged.
- **Membership is one column.** A quiet check used to load the whole
  communities row to see `join_status`. It now selects that column, then the
  replay-session id if needed.
- **Public fast tier is 5 minutes, members stay at 2.** A community we have
  not joined and have no session for waits `anon_fast_interval` (default 300
  seconds) between successful checks. Members (joined, or a stored session)
  stay on `--fast-interval` (120 seconds). The slow tier stays at 15 minutes.
  Error backoff is the same formula as before. On today's list that is about
  15,000 fewer public checks a day. A new public post is noticed in about
  five minutes instead of two. Member feeds are unchanged, and the watcher's
  Circle budget (40 requests/minute) has more room for them.

  Override, first match wins, values under 30 seconds are ignored:

  1. Database setting `watch_anon_fast_interval` (read every 5 minutes, no
     restart).
  2. Environment `WATCH_ANON_FAST_INTERVAL` (read at start and on that same
     5-minute reload; a change in `/etc/warmr/warmr.env` needs a restart of
     `warmr-watcher`).
  3. `circle-leads watch --anon-fast-interval SECONDS` pins the value for
     that process and ignores the setting and the env.
  4. Default 300.

  If a week of the usage chart still annualizes above 5 GB, set the database
  key to `600` or `900`. That only slows public communities.
- **Cookie blobs are cached for 10 minutes** in the watcher process
  (`WATCH_COOKIE_CACHE_TTL`, seconds, default 600, `0` turns it off). The
  average blob is about 2 KB and a member check used to download it every
  cycle. `store_session` drops that host's entry. A feed that refuses the
  cached cookies re-reads the row before anyone is told the session died, so
  a paste on the dashboard is not reported dead for the whole TTL. Scans use
  `load_cookies_strict` and do not use the cache.
- **Heartbeat writes do not read the old value.** `set_setting` is an UPDATE,
  then an INSERT only when the key is new. The runtime snapshots are about
  1 KB and were downloaded on every beat.
- **Harvest catalogue** loads the eight columns `_community_hosts` uses, not
  the descriptions and ICP notes.
- **A repeated post** is found by its hash. The post body and the
  classification audit stay off that SELECT until the text actually changed.
- **`due_states` and the half-hourly watch-list repair** no longer load
  columns they do not read.
- **Connections are recycled every 30 minutes** instead of every 5
  (`pool_recycle=1800`), with `pool_pre_ping` still on. Each recycle is a new
  Supavisor login. This is a small part of the byte total; it cuts connection
  churn. A connection the pooler has already closed is dropped on the next
  checkout, not reused.
- **The first watch-list sync no longer waits out the machine's uptime.**
  The timer compared `time.monotonic()` to zero, so a host that had been up
  for less than 30 minutes skipped the startup sync. The first pass always
  syncs now.

## Prepared statements stay off

Port 6543 is Supavisor in transaction mode. The server connection is handed
back at the end of each transaction, so a named prepared statement created by
psycopg does not exist on the next one (`prepared statement does not exist`).
`prepare_threshold` remains `None` for any `*.pooler.supabase.com` host or
port 6543. Session mode (port 5432) would allow them and would also bring back
the 15-connection cap this pool was built to avoid. The RowDescription cost
is cut by selecting fewer columns, which is the part that shows up as egress.

## Check after a deploy

Do this a day later, not in the same minute. `pg_stat_statements` is a
cumulative counter since 2026-09-02, so compare a delta, not the lifetime
total. Snapshot before and after:

```sql
SELECT calls, rows,
       left(regexp_replace(query, '\s+', ' ', 'g'), 160) AS q
FROM pg_stat_statements
WHERE query ILIKE '%watch_state%'
   OR query ILIKE '%FROM communities%'
   OR query ILIKE '%replay_sessions%'
   OR query ILIKE '%FROM settings%'
ORDER BY calls DESC
LIMIT 30;
```

What should slow down:

- `SELECT watch_state.id AS watch_state_id, ...` (the full ORM load) stops
  increasing. The finish query names `mode`, `tier`, `consecutive_errors`
  and, only when ids were appended, `recent_ids`.
- `SELECT communities.id, communities.slug, ... WHERE communities.id = $1`
  (the full row in `_is_member`) stops increasing. The replacement selects
  `communities.join_status`.
- `SELECT replay_sessions....encrypted_cookies` for a given host falls from
  about once per two minutes to about once per ten minutes.
- `SELECT settings.key, settings.value, ...` no longer runs once per
  heartbeat write. Heartbeats are `UPDATE settings` only.

In the Supabase Usage dashboard, daily egress times 30 should sit under
5 GB. The planning estimate from the measurements above is about 2.5–4.5 GB
a month off the current run-rate (payload only is the low end; counting the
RowDescription on the wide lookups is the high end). The chart is the number
that matters. No schema change and no production data change are part of this.

## Deploy and roll back

The droplet does not git-pull. `deploy/droplet/deploy_release.sh` archives
the commit, unpacks it under `/opt/warmr/releases/<sha>`, builds that
release's venv, runs `circle-leads check-schema` with `/etc/warmr/warmr.env`,
points `/opt/warmr/current` and `/opt/warmr/watcher` at it, and restarts
`warmr-worker`, `warmr-watcher`, `warmr-recovery` and `warmr-communities`.
It does not restart `warmr-browser` and does not enable `warmr-join.timer`.
The next join run picks up `/opt/warmr/current` on its own. Nothing here
needs a new env var; leave `WATCH_ANON_FAST_INTERVAL` unset to keep 300
seconds.

From a checkout that can SSH to the droplet, after this is on `main`:

```sh
CIRCLE_DEPLOY_HOST=root@157.230.181.187 \
CIRCLE_DEPLOY_SSH_IDENTITY="$HOME/.ssh/id_ed25519_warmr_ops" \
  deploy/droplet/deploy_release.sh <sha-on-main>
```

Use the SSH host and key you already use if those differ (`warmr-1` and
`~/.ssh/warmr_droplet` are the older names for the same script). The script
is the deploy. Roll back by running it again with the previous release sha:
the old tree is still in `/opt/warmr/releases/<previous>`, and the same four
units restart onto it. A `watch_anon_fast_interval` row, if you added one,
is ignored by the old code. The dashboard on Vercel follows `main` through
its existing Git integration; do not `vercel deploy --prod` from a local
tree.
