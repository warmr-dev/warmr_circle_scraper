# openclaw-warm-3000-10203 (openclawlaunch.com, Lite)

Where the worker and the watcher run since 2026-09-29. The droplet `warmr-1` is
powered off, not deleted; [`../droplet/README.md`](../droplet/README.md) is now
the rollback runbook.

| Unit | What it does | Request budget |
|---|---|---|
| `warmr-worker` | drains the scan-job queue, scheduled harvest, enrichment, ICP | `state/circle-governor.json`, 20/min, 1200/h |
| `warmr-watcher` | polls community feeds, triages new posts | `state/watch-governor.json`, 40/min, 2400/h |

**Joins do not run here.** See [What does not work](#what-does-not-work).

## The one thing to remember

**Only `~/.openclaw` survives.** The home directory, `/opt`, `/app` and every
apt package live on the container's overlay filesystem and are gone when the
instance is replaced — and instances *are* replaced: the first one we were given
vanished with everything on it, ours was a different container by the next day.

Everything of ours therefore lives under `~/.openclaw/warmr`:

    ~/.openclaw/warmr/releases/<sha>/   the tracked tree of that commit
    ~/.openclaw/warmr/current           symlink to the release in use
    ~/.openclaw/warmr/venv/             one venv, 321 MB
    ~/.openclaw/warmr/browser/          Playwright's Chromium, 658 MB (unused, see below)
    ~/.openclaw/warmr/etc/warmr.env     secrets, mode 600
    ~/.openclaw/warmr/state/            governor counters, pid files, SQLite fallback
    ~/.openclaw/warmr/bin/              the scripts in this directory
    ~/.openclaw/warmr/log/              logs — there is no journal here

`bootstrap.sh` puts back the apt packages (`python3.11-venv`, `xvfb`, Chromium's
libraries) when the container has been replaced. It runs before the supervisor
on every tick and does nothing when they are already installed.

## Access

Real `ssh`, `scp` and `sftp` — over Tailscale, which is how this host exposes
them; no inbound port is opened. The Mac must be signed in to the same tailnet.

    ssh node@100.126.5.23

The instance was joined to the tailnet with a one-shot auth key pasted into the
vendor's SSH panel (Reusable off, Ephemeral off — ephemeral would remove the
device while the instance sleeps).

## Supervision: there is no init

PID 1 is `node openclaw.mjs gateway` — the vendor's own agent gateway. There is
no systemd, no s6, no cron. Nothing starts our processes, and nothing restarts
them.

What we use instead is the gateway's own scheduler, which runs plain shell (no
model, no tokens):

    openclaw cron add --every 1m --best-effort-deliver \
      --command "sh ~/.openclaw/warmr/bin/bootstrap.sh && sh ~/.openclaw/warmr/bin/ensure-services.sh" \
      warmr-services

Two things that cost a debugging round each, both measured on 2026-09-29:

- **`--best-effort-deliver` is not optional.** Without it the job tries to
  deliver its result to a chat channel, finds none configured, and fails
  *closed* — the command never runs at all. The job sat scheduled "every 1m"
  for twelve hours without executing once.
- **A dead service becomes a zombie, and a zombie answers `kill -0`.** With no
  init to reap orphans, a killed worker stayed in the process table as
  `<defunct>` and the first supervisor read it as healthy. `ensure-services.sh`
  therefore reads `State:` from `/proc/<pid>/status` and checks the command
  line, rather than trusting that the pid exists.

Verified end to end: `kill -9` on the worker, back up 76 seconds later, put
there by the gateway and not by a human.

## Limits

| | |
|---|---|
| Memory | 2 GiB (`memory.max`), no usable swap |
| CPU | 1 core (`cpu.max` = `100000 100000`) |
| Disk | 10 GB on `~/.openclaw`; ~1.1 GB used by us |
| Outbound | IMAP 993 and Postgres 6543 are open — checked, because many hosts allow 443 only |

`free` and `nproc` report the host machine (32 GB, 12 cores). They are not our
quota; read the cgroup files.

## Database

This host connects as `warmr_host`, not as the table owner: SELECT/INSERT/
UPDATE/DELETE on `public`, no DDL. It needs `BYPASSRLS` because every table in
`public` has row level security on with no policies, so an ordinary role reads
nothing at all.

## What does not work

**The join browser.** Chrome cannot keep its sandbox here: the container refuses
to create the namespaces it needs, and the setuid helper that solved this on the
droplet does not help, because the block is on namespace creation itself, not on
setuid. Running with `--no-sandbox` is not an option for a browser that opens
pages written by strangers. Chromium is installed (658 MB) only so that a future
host with the right capabilities needs no rebuild.

Joins are therefore paused. `docs/joining.md` describes a path that currently
has nowhere to run.

**Alerts on a dead service.** `OnFailure=` was a systemd feature. The heartbeat
in the database and the Vercel `/api/watchdog` cron still work and are
host-agnostic — that is now the only thing that notices a dead machine.

## Deploying

    SHA=$(git rev-parse --short origin/main)
    ssh node@100.126.5.23 "mkdir -p ~/.openclaw/warmr/releases/$SHA"
    git archive --format=tar origin/main | ssh node@100.126.5.23 "tar -x -C ~/.openclaw/warmr/releases/$SHA"
    ssh node@100.126.5.23 "ln -sfn ~/.openclaw/warmr/releases/$SHA ~/.openclaw/warmr/current"
    # apply any pending migration by hand first -- SKIP_DB_INIT=true means the
    # code never adds a column it needs, it just fails
    ssh node@100.126.5.23 "rm -f ~/.openclaw/warmr/state/*.pid"   # the supervisor restarts both within a minute

Keep exactly one release: the disk is 10 GB, and a venv is 321 MB.

## Rolling back to the droplet

The droplet is powered off with a snapshot taken on 2026-09-29
(`warmr-1-pre-openclaw-2026-09-29`), and its units are disabled, so powering it
on does not start a second worker against the same database. To go back: power
it on, `systemctl enable --now warmr-worker warmr-watcher`, and stop the
automation here (`openclaw cron rm warmr-services`) so the two do not both run.
Note the droplet keeps its IP only while it exists; a restore from the snapshot
gets a new address and the Circle session has to be established again.
