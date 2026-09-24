# warmr-1 (DigitalOcean, sgp1)

Two services, one droplet, one address.

| Unit | What it does | Request budget |
|---|---|---|
| `warmr-worker` | drains the scan-job queue, runs the scheduled harvest, enrichment, ICP | `circle-governor.json`, 20/min |
| `warmr-watcher` | polls community feeds, triages new posts | `watch-governor.json`, 40/min |

## Layout

    /opt/warmr/releases/<sha>/   a release: the tracked tree of that commit, plus its own venv
    /opt/warmr/current           symlink to the release the services run
    /etc/warmr/warmr.env         secrets, 0640 root:warmr
    /var/lib/warmr               state: governor files, nothing precious

No Circle credentials live here: nothing on this host signs in to Circle. The
DigitalOcean token is not here either -- it could delete the droplet it sits on.

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
