# 2026-11-27 — Cut Supabase egress before the fair-use date

Supabase project `qxpewmsujqtbwhpddlti` (free plan) was on course for more
than 5.5 GB of egress in a month. The database is about 200 MB; the traffic
is the same rows read on every watcher check, every heartbeat and every
harvest pass, through the transaction pooler. Fair use starts applying on
2026-12-27.

The watcher still re-reads mode, tier and the counters when it finishes a
check, so a join or a tier change made mid-cycle is what gets scheduled.
It no longer re-reads `recent_ids` unless this check appended ids, and it
no longer loads a communities row to see `join_status`. Public communities
on the fast tier wait 5 minutes (setting `watch_anon_fast_interval`, env
`WATCH_ANON_FAST_INTERVAL`, or `--anon-fast-interval`; default 300). Members
stay at 2 minutes. Cookie blobs are cached in the process for 10 minutes.
Heartbeat writes, the harvest catalogue and unchanged-post lookups no longer
pull columns they do not use. Pool recycle went from 5 minutes to 30, with
pre-ping left on. Prepared statements stay disabled: transaction mode on
port 6543 cannot keep them.

No schema change, no production data change, no deploy in this change.
How to read the statement stats after a deploy, and how the droplet release
script restarts the four units, is in `docs/supabase-egress.md`.

- Local: implemented and unit-tested in the task worktree.
- Committed: this branch.
- PR: opened for review. Not merged.
- Merged: no.
- Deployed/Applied: no. Droplet and Vercel are unchanged.
- Live verified: no. `pg_stat_statements` was read only to size the queries.
- Workspace closed: no.
