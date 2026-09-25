# warmr-1 (DigitalOcean, sgp1)

Three services, one droplet, one address.

| Unit | What it does | Request budget |
|---|---|---|
| `warmr-worker` | drains the scan-job queue, runs the scheduled harvest, enrichment, ICP | `circle-governor.json`, 20/min |
| `warmr-watcher` | polls community feeds, triages new posts | `watch-governor.json`, 40/min |
| `warmr-browser` | holds the join session: headed Chrome in a virtual display, CDP on `127.0.0.1:9222` | driven per join, see [`joining.md`](../../docs/joining.md) |

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
symlink and restart. The order matters: `SKIP_DB_INIT=true` means the code
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
