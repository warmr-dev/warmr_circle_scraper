# warmr-1 (DigitalOcean, sgp1)

Three services, one droplet, one address.

| Unit | What it does | Request budget |
|---|---|---|
| `warmr-worker` | drains the scan-job queue, runs the scheduled harvest, enrichment, ICP | `circle-governor.json`, 20/min |
| `warmr-watcher` | polls community feeds, triages new posts | `watch-governor.json`, 40/min |
| `warmr-browser` | holds the join session: headed Chrome in a virtual display, CDP on `127.0.0.1:9222` | driven per join, see [`joining.md`](../../docs/joining.md) |
| `warmr-join` (+ `.timer`, every 4 h) | works the join queue as account 4 in `warmr-browser`; reads the emailed code from the bot mailbox | the worker's `circle-governor.json` |

## Layout

    /opt/warmr/releases/<sha>/   a release: the tracked tree of that commit, plus its own venv
    /opt/warmr/current           symlink to the release the services run
    /etc/warmr/warmr.env         secrets, 0640 root:warmr
    /var/lib/warmr               state: governor files, nothing precious
    /opt/warmr/browser-trial/    the join browser: its own venv, its own Chromium, its own profile
    /etc/warmr/warmr-join.env    the bot's Circle and mailbox credentials, 0640 root:warmr

Since 2026-09-25 this host **does** sign in to Circle, as the bot account, to
join communities: `/etc/warmr/warmr-join.env` holds `CIRCLE_EMAIL4`,
`CIRCLE_PASSWORD4` and the mailbox app password, readable only by `warmr`.
No customer or personal Circle account is on this host, and the debugging
port is bound to localhost. The DigitalOcean token is not here either -- it
could delete the droplet it sits on.

## Deploying

From a checkout of `main` on a machine that has the repo:

    git archive --format=tar origin/main | ssh root@<ip> \
      "install -d -o warmr -g warmr /opt/warmr/releases/<sha> && tar -x -C /opt/warmr/releases/<sha>"

then build the venv, **apply any pending migration**, and only then switch the
symlink and restart. `deploy/droplet/deploy_release.sh [ref]` does all of it
from a Mac, and refuses to switch when `circle-leads check-schema` finds a
column the database lacks. It installs the join units but leaves the timer
off: turning joins on is a person's call. The order matters: `SKIP_DB_INIT=true` means the code
never creates its own columns, so deploying code that knows about a column the
database does not have turns into a silent failure -- that is exactly what
happened with `read_outcome` on 2026-09-23.

## Alerts

`OnFailure=warmr-alert@%n.service` on each unit sends the last lines of the
journal to Telegram when a service dies. That cannot report a dead machine, so
each service also stamps a heartbeat into the database and the dashboard's
`/api/watchdog`, run by a Vercel cron every ten minutes, shouts when a stamp
goes stale.

## The join browser

`warmr-browser` is not part of a release: it lives in `/opt/warmr/browser-trial`
with its own venv and its own Chromium, so a bad deploy of the worker cannot
take the Circle session with it. It must run **headed under `xvfb-run`** --
headless is what Cloudflare stops -- and it keeps Chrome's sandbox through the
setuid `chrome_sandbox` helper, because Ubuntu 24.04 blocks the namespace
sandbox for unprivileged users. The full story, including what has and has not
been verified, is in [`docs/joining.md`](../../docs/joining.md).


## Independent maintenance after long-harvest starvation

Install `warmr-recovery.service` and `warmr-communities.service` alongside the
worker and watcher. The worker runs with `--external-maintenance`; its default
CLI mode retains the old single-process schedule for compatibility. Run exactly
one instance of each maintenance lane against a database. Recovery never claims
scan jobs or resets historical classification flags.

- Recovery checks due durable classification retries (at most 25) and previously
  attempted transport errors (at most 25) each loop, with independent error
  handling. Existing 1/5/15/60-minute backoff is retained. A batch may take longer
  than the nominal 60-second polling interval; harvest cannot delay it.
- Export retry selects only unsynced, nonduplicate LEADs whose post is classified,
  source timestamp is within 48 hours, and prior error attempt is recent and due.
  Normal payload/audit gates and stable ingress identity still apply. Held,
  rejected, acknowledged and never-attempted rows are excluded. No historical replay.
- Communities runs the existing due enrichment/ICP and join-type batches. It
  shares the worker's cross-process Circle governor file; request budgets,
  schedule settings and auto-join approval gates remain unchanged.
- Each lane writes its own runtime/heartbeat plus durable stage finish/error.
  Needs attention reports a lane that stops after its first heartbeat.

The release script installs all four runtime units and checks their active state.
It does not enable the join timer or browser. Explicitly select the new target:

```sh
CIRCLE_DEPLOY_HOST=root@157.230.181.187 \
CIRCLE_DEPLOY_SSH_IDENTITY="$HOME/.ssh/id_ed25519_warmr_ops" \
  deploy/droplet/deploy_release.sh <reviewed-commit>
```

For rollback to a release without maintenance delegation, stop and disable
`warmr-recovery` and `warmr-communities`, restore the previous worker unit, then
switch the release symlinks and restart worker/watcher. This prevents concurrent
owners of the legacy schedule. No migration is required by this patch.

Attention no longer treats old/empty connected scans as proof of live cookies.
Readable scans must be within 24 hours and follow the stored credential update.
Access denied may reflect authentication or community permissions; the dashboard
cannot establish which from a connected flag alone. Classification failures are
grouped by their actual error cause, and parsed descriptions survive in the audit.
