# Joins back on the droplet, with the bot's sign-in carried from the Mac

## Problem and change

Joins ran on `warmr-1` from 2026-09-29. When that droplet was replaced by
`circle-bot-prod` in October only the worker and the watcher were moved; the
join browser, the bot's credentials and the timer were not, and joins stopped
on 2026-10-05. On 2026-11-06 the browser was set up again, but Circle's central
login (`login.circle.so`) shows this address a Cloudflare Turnstile checkbox
after the email, so the bot could not sign in there and joins ran from the Mac,
which depends on the laptop being on.

On 2026-11-09 the bot's Circle cookies were carried from the Mac's bot Chrome
into the droplet's browser. Opening a community through that central session
needs no email: four joined communities opened as a member with no check, and
the real walk re-entered steepinsights and stored its session. Joins run on
the droplet again, every 15 minutes, one community a run, as on the Mac; the
Mac's schedule is off.

- `deploy/droplet/carry_bot_session.py`: `export` (Mac) piped into `import`
  (droplet). Only Circle's cookies, never Cloudflare's, never other sites';
  the values pass only through the pipe. Nothing about the checkbox is
  touched: when the central session ends, a full sign-in on the droplet still
  stops as `challenge_stop`, and the carry is run again.
- `warmr-join.timer` every 15 minutes, `warmr-join.service` `--limit 1` and a
  30-minute timeout (was every 4 hours, 5 a run, 90 minutes).
- `warmr-browser.service` names the Chromium build installed on the droplet
  (`chromium-1248`; the unit still said `1243`).

## Scope and operations

Units and an operator script. `deploy_release.sh` installs the join units on
every release; until this is merged and deployed, the droplet runs the same
unit files installed by hand on 2026-11-09 (`WORKLOG.md`). The browser unit is
not installed by the release; the droplet's copy already named `chromium-1248`.
No migration, no new configuration.

Documentation added: `docs/joining.md`, `deploy/droplet/README.md`
Delivery record: `docs/changes/2026-11-09-joins-back-on-the-droplet.md`

## Verification and delivery

```sh
python -m pytest tests/test_carry_bot_session.py -q
```

On `circle-bot-prod`, 2026-11-09: 195 cookies carried, 0 refused by the
browser, still there after a browser restart; `login.circle.so/sign_in?
request_host=` opened steepinsights, joindpp, autohive and startup-ready as a
member with no Cloudflare check; `_run_batch` re-entered steepinsights
(`joined`, session stored); `systemctl start warmr-join` with the new units:
`Attempted 0` (empty queue), result success. No new community has been joined
from this droplet yet.
