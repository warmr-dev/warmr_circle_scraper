# Joining a Circle community

How a bot account gets into a free Circle community: what runs, where it runs,
what the code refuses to do, and which claims here are measured.

Written 2026-09-25. Every number below is a measurement. Where something has
not been measured, the document says so instead of guessing.

- [The short version](#the-short-version)
- [Why the work is split](#why-the-work-is-split)
- [The parts](#the-parts)
- [The funnel](#the-funnel)
- [States](#states)
- [Rules the code will not break](#rules-the-code-will-not-break)
- [Where it runs](#where-it-runs)
- [Running one join by hand](#running-one-join-by-hand)
- [What is verified, and what is not](#what-is-verified-and-what-is-not)
- [Troubleshooting](#troubleshooting)

---

## The short version

A Python driver walks the five steps every Circle community shares: find the
login form, type the password, read the six-digit code out of the bot's
mailbox, click the join gate, confirm membership through Circle's own API. It
drives a browser that is **already open** on the droplet, inside a virtual
display. When the driver meets a page it cannot name, it stops and hands that
community to an LLM agent or to a person.

The driver costs nothing per visit. The agent costs $0.6–0.9 per community.
That difference is the entire reason the split exists.

```
   queue (ICP-fit, free_join, not_attempted)
                 │
                 ▼
     ┌───────────────────────┐   knows the page   ┌──────────────┐
     │  cdp_driver (Python)  │ ─────────────────▶ │  membership  │
     └───────────────────────┘                    │   verified   │
                 │                                └──────────────┘
                 │ UNKNOWN / profile form
                 ▼
     ┌───────────────────────┐
     │  agent, or a person   │
     └───────────────────────┘
```

**How a community gets into the queue.** It needs its own address
(`communities.host`) — a `discover.circle.so/products/…` card names none. Only
rows with an address get the join-type check (`join_type_schedule`, worker) and
only those can be queued (`reach.real_host`). A web-search find stores the
host of its link (`discovery/persist.py`); a link added on the dashboard goes
through `get_or_create_community`, which does the same. Until 2026-11-09 search
finds were stored without one, so none of them ever reached the queue; 73 rows
were filled in by hand that day (`docs/changes/2026-11-09-discovery-saves-host.md`).

## Automatic runs on the droplet (since 2026-11-09)

Joins run on `circle-bot-prod` again, as they did on `warmr-1` from 2026-09-29
until that droplet was replaced in October (only the worker and the watcher
were moved then; the join browser was not). `warmr-join.timer` starts
`circle-leads auto-join --driver cdp --account 4 --limit 1` every 15 minutes
in `warmr-browser` (`deploy/droplet/`).

| thing | where |
|---|---|
| schedule | `warmr-join.timer`, every 15 minutes |
| code | `/opt/warmr/current` (each release; `deploy_release.sh` installs the units) |
| browser | `warmr-browser.service`: Chromium in a virtual display, CDP on `127.0.0.1:9222` |
| browser profile | `/opt/warmr/browser-trial/profile` (holds the bot's sign-in) |
| attempt log | `/var/lib/warmr/data/join_attempts.log` (daily cap, rest after a handoff) |
| logs | `journalctl -u warmr-join`, screenshots of every stop in `/var/lib/warmr/join-shots/` |

- **One community a run.** A new community is joined within about 15 minutes,
  a backlog at most 4 an hour, and the 25-a-day cap per account still holds.
  About 94 visits from one account in a day brought 2FA challenges on
  2026-09-16. An empty queue ends the run at once ("Attempted 0").
- **The bot's sign-in comes from the Mac.** Since `circle-bot-prod`, Circle's
  central login (`login.circle.so`) shows this address a Cloudflare Turnstile
  checkbox after the email, so this browser cannot sign the bot in by itself;
  from the Mac the check passes by itself. Opening a community through the
  central session the browser already holds needs no email and met no check
  (2026-11-09: steepinsights, joindpp, autohive, startup-ready). So the bot is
  signed in on the Mac and its Circle cookies are carried over:

  ```sh
  # on the Mac, the bot's Chrome open (hermes-chrome/acct4, CDP 127.0.0.1:9222)
  python deploy/droplet/carry_bot_session.py export \
    | ssh root@157.230.181.187 /opt/warmr/current/venv/bin/python \
        /opt/warmr/current/deploy/droplet/carry_bot_session.py import
  ```

  Only Circle's cookies go (`login.circle.so`, the communities, `*.circle.so`);
  the profile's Google and other cookies stay on the Mac, and Cloudflare's own
  cookies never move. The values pass only through the pipe. Nothing about the
  checkbox is touched: when the central session ends, a full sign-in here stops
  as `challenge_stop` (a screenshot in `join-shots/`), and the fix is to sign
  the bot in on the Mac and run the two lines again.
- **Two machines never run at once.** Each counts the daily cap from its own
  attempt log, so two would double it. The Mac's schedule is off while the
  droplet's runs; moving back is the reverse (below).

### Bringing joins back on a new droplet

What `warmr-1` had and its replacement lacked, in order:

1. `deploy_release.sh` -- the release, with Playwright; it installs
   `warmr-join.service` and `.timer` and leaves the timer off.
2. The browser: a venv in `/opt/warmr/browser-trial` with the release's
   Playwright version, `playwright install chromium` and `install-deps`,
   `chrome_sandbox` set to `root:root 4755`, then `warmr-browser.service`
   from `deploy/droplet/` with its `chromium-NNNN` path matching the installed
   one (see "Where it runs").
3. `/etc/warmr/warmr-join.env` (`root:warmr 640`): `CIRCLE_EMAIL4`,
   `CIRCLE_PASSWORD4`, `CIRCLE_MAIL_USER`, `CIRCLE_MAIL_APP_PASSWORD`.
4. The bot's sign-in, carried from the Mac (above).
5. The attempt log from the machine that ran joins until now, into
   `/var/lib/warmr/data/join_attempts.log`, owned by `warmr`.
6. A dry run (`auto-join ... --dry-run`, the command `deploy_release.sh`
   prints), the Mac's schedule off, then `systemctl enable --now warmr-join.timer`.

### What the walk does, wherever it runs

- **The walk** is `circle_leads/join/cdp_join.py`: the Ego driver's state
  machine (`ego_join_driver.mjs`), ported rule for rule to Python and driven
  over CDP.
- **The emailed code** is read from the bot mailbox while the page waits, and
  typed in the same visit: at sign-in (`/two_fa`) and on the join step alike.
  A code that never arrives stops the visit as `login_code_needed` (before a
  membership, a handoff) or `email_code_needed` (after it, `profile_pending`).
- **The profile questions** are answered from `join_form_answers`
  (`forms.resolve_form`) in the same visit; a question the bot may not answer
  stops it as `profile_incomplete`, naming the question.
- **Outcomes** go to the database: `join_status`, the account in
  `communities.join_account`, the session through `connect_host`, every visit
  in `activity_log` (`join`), a handoff in `join_status_detail`.
- **A handoff rests for a week** (`reach.HANDOFF_REST`). A community the bot
  stopped on -- a question it may not answer, a page it could not name --
  sits out the automatic runs for 7 days and is not counted as waiting on the
  dashboard; `--host` retries it at once. Before this, every run walked the
  same few stuck hosts again, each visit mailing a new login code.
- **Enter through the central session first.** A full sign-in (email, code)
  on a community behind Circle's central login ended the account's sessions in
  the other such communities: on 2026-11-09 six re-entries in a row left only
  the last session alive. Opening `login.circle.so/sign_in?request_host=<host>`
  while the browser still holds the central session lands on the community
  signed in and ended none. The walk tries that first and signs in fully only
  when it does not work. Communities with their own login (la-fusee, hero,
  hailerz) were not affected.
- **Circle's own profile-step pieces**: the "I agree to the terms and privacy
  policy" box is ticked; a "Location" field (a place search, `field_type`
  `location`) gets the persona's `location`, typed in and picked from Circle's
  suggestions -- never a place the model made up.
- **The central login is never the community.** `login.circle.so` answers
  `/internal_api/pundit_users` for the account's own space; it is not read as
  membership, and its cookies are not stored as a community session.

## On the Mac (2026-11-06 to 2026-11-09, now the fallback)

While the droplet had no sign-in, the same walk ran on the Mac against a
dedicated Google Chrome on its own profile. It is installed and switched off:

| thing | where |
|---|---|
| schedule | `~/Library/LaunchAgents/com.warmr.autojoin.plist`, every 15 minutes (disabled) |
| launcher | `~/Library/Application Support/warmr/worker/run_autojoin.sh` |
| code | `venv-cdp` there, a non-editable install of the repo (launchd cannot read `~/Documents`) |
| Chrome profile | `~/Library/Application Support/warmr/hermes-chrome/acct4`, CDP on `127.0.0.1:9222` |
| attempt log | `data/join_attempts.log` there |
| logs | `~/Library/Logs/warmr/autojoin.log`, `autojoin.err`, `join-shots/` |

The launcher asks the queue first (`--dry-run`) and starts Chrome only when
there is something to join. To move joins back here: `systemctl disable --now
warmr-join.timer` on the droplet, copy its attempt log over the Mac's, then
`launchctl enable gui/$(id -u)/com.warmr.autojoin` and `launchctl bootstrap
gui/$(id -u) ~/Library/LaunchAgents/com.warmr.autojoin.plist`. The Mac's Ego
runs (`--driver ego`, still the default) have been off since 2026-09-29.

## Why the work is split

Measured on 2026-09-25, one community per run, driven end to end by the Hermes
agent through the same browser. Cost from two independent sources that agree:
OpenRouter's daily usage counter, and Hermes's own per-session estimate in
`~/.hermes/state.db`.

| run | model | skill file | joined | cost |
|---|---|---|---|---|
| agent alone | Sonnet | none | 1 | **$0.94** |
| agent + hand-written skill | Sonnet | `warmr/circle-join` | 1 | **$0.64** |
| cheaper model | Gemini 3 Flash | none | 0 | **$0.57** |
| driver in this repo | — | — | 1 | **$0** |

Three things that measurement settled:

- **The agent does not get cheaper by repetition.** In one-shot mode
  (`hermes chat -q`, which is how a queue would call it) Hermes writes no skill
  files and no memory entries — verified by diffing `~/.hermes` around a run.
  Every visit re-derives the same five steps from scratch.
- **Most of the bill is cache reads** of that re-derivation: 2.25M cache-read
  tokens against 14k output tokens for the Sonnet runs.
- **A cheaper model is not the answer.** Gemini 3 Flash spent $0.57 over four
  sessions and joined nothing.

Hand-writing the recipe as a skill cut the bill by about a third. Writing it as
code cut it to zero. The agent keeps its place for pages the code cannot name,
where a fixed recipe has nothing to say.

## The parts

| file | job |
|---|---|
| `circle_leads/join/cdp_driver.py` | the state machine and the five steps; attaches over CDP, never launches or closes a browser, never writes to the DB |
| `circle_leads/join/email_code.py` | pulls Circle's six-digit login code out of the bot mailbox over IMAP, read-only |
| `scripts/join_one.py` | runs one community end to end and prints where it stopped; persists nothing |
| `warmr-browser.service` (droplet) | the browser both the driver and the agent attach to |
| `/etc/warmr/warmr-join.env` (droplet) | the bot's Circle and mailbox credentials |

The driver and the agent share one browser, so they share one Circle session:
whoever logs in, both are logged in.

## The funnel

```
  attach to the running browser (CDP, 127.0.0.1:9222)
        │
        ▼
  goto(url) ──▶ assess() ──── member? ──▶ MEMBER ──▶ done, nothing to do
        │                        │
        │                        no
        ▼                        ▼
  LOGIN_WALL / JOIN_GATE ──▶ sign_in()
        │                        │
        │                        ├─ host is not Circle's ──▶ EXTERNAL_LOGIN (stop, hand over)
        │                        │
        │                        ▼
        │                    TWO_FA ──▶ fetch_login_code()  (IMAP, waits up to 5 min)
        │                        │            │
        │                        │            └─ no code ──▶ stop
        │                        ▼
        │                  enter_code()
        ▼                        │
  JOIN_GATE ◀────────────────────┘
        │
        ▼
  click_join() ──▶ PROFILE_STEP ──▶ agent, or a person
        │
        ▼
  assess() ──▶ /internal_api/spaces says is_space_member ──▶ joined
```

Two details that cost a visit each when they were missing, and are now in the
code with the community they were found on:

- A community's home page usually carries **no login form**. Circle serves it
  at `/users/sign_in`, which may render a *method chooser* ("Sign in with an
  email") with zero input fields until it is clicked, and may render the form a
  second after the navigation returns (`thefpahub`, `generouslifeapp`).
- Circle finishes the login **asynchronously**: the page still shows the
  sign-in form when `networkidle` returns and only then swaps to `/feed`.
  `sign_in()` waits for the URL to leave the form instead of believing the
  first look.

## States

`classify()` names the page; `assess()` overrides it with the API answer,
because a community we had already joined still rendered a "Join" button.

| state | what it means | what happens |
|---|---|---|
| `MEMBER` | `/internal_api/spaces` reports `is_space_member` | done |
| `LOGIN_WALL` | a login link, no join gate | `sign_in()` |
| `JOIN_GATE` | a join/register button | `click_join()` |
| `TWO_FA` | `/two_fa`, six `input[type=tel]` boxes | code from the mailbox, `enter_code()` |
| `PROFILE_STEP` | `/settings/profile?new_state=true` | agent or person; the driver will not invent answers |
| `EXTERNAL_LOGIN` | the login lives on a host that is not Circle's | stop, hand to a person |
| `CLOUDFLARE` | the challenge interstitial | stop |
| `UNKNOWN` | anything else | hand to the agent — this is what the agent is for |

## Rules the code will not break

- **Credentials go only to Circle.** The community's own host, or `*.circle.so`.
  A host reached by redirect is accepted only if it answers Circle's internal
  API (`speaks_circle_api`). This rule exists because an asset-based check let
  the password reach an auth0 page on 2026-09-21.
- **A 200 from `/internal_api/spaces` is not membership.** Open communities
  serve that list to strangers. Only `is_space_member: true` on at least one
  space counts.
- **The mailbox is read-only.** `BODY.PEEK`, `readonly=True`, and only mail
  addressed *to* the bot and newer than the moment the login started. Nothing
  is marked read, nothing is deleted.
- **The driver never launches or closes the browser.** It attaches and
  detaches; closing would take the agent's browser down with it.
- **The driver never writes to the database.** Outcomes are printed; a person
  or a caller decides what to store.
- **A form only a person can answer is `needs_human`.** `icecampus` asks for a
  passport or ID number, date of birth, phone and home address. The bot does
  not answer that, and the status says so rather than pretending the community
  is unreachable.

## Where it runs

DigitalOcean droplet `circle-bot-prod` (`157.230.181.187`, since October
2026; `warmr-1` before it), Ubuntu 24.04.5, 3.9 GB RAM, 2 CPUs, no swap.

| thing | path |
|---|---|
| service | `/etc/systemd/system/warmr-browser.service` (enabled, `Restart=always`) |
| code + venv + Chrome | `/opt/warmr/browser-trial/` (Chromium `ms-playwright/chromium-1248`) |
| Chrome profile (the session) | `/opt/warmr/browser-trial/profile` |
| credentials | `/etc/warmr/warmr-join.env`, `root:warmr`, mode `640` |
| CDP | `127.0.0.1:9222` — localhost only, never exposed |

Three things about that box that took a while to get right:

- **Headed, not headless.** Cloudflare challenged headless Chrome on 3 of 3
  communities from this droplet and challenged none of 3 when the same binary
  ran headed inside `xvfb-run`. The blocker was the headless mode, not the
  datacenter IP. This supersedes the verdict in
  [`remote_browser_poc.md`](remote_browser_poc.md), which tested headless only.
- **The sandbox stays on.** Ubuntu 24.04 sets
  `kernel.apparmor_restrict_unprivileged_userns=1`, so Chrome's namespace
  sandbox aborts at startup for an unprivileged user. The fix is the setuid
  `chrome_sandbox` helper plus `CHROME_DEVEL_SANDBOX`, not `--no-sandbox`: this
  browser opens pages written by strangers.
- **It runs as `warmr`, not root,** and is capped at `MemoryMax=1500M` because
  the worker and the watcher share the same 3.9 GB.

## Running one join by hand

On the droplet, with the timer's own environment (`--host` matches the slug
or part of the URL, and skips the week's rest after a handoff):

```bash
ssh root@157.230.181.187
systemctl is-active warmr-browser            # the browser must already be up
curl -sf http://127.0.0.1:9222/json/version  # CDP answers
systemd-run --wait --pipe --collect -p User=warmr -p WorkingDirectory=/var/lib/warmr \
  -p EnvironmentFile=/etc/warmr/warmr.env -p EnvironmentFile=/etc/warmr/warmr-join.env \
  -p Environment=CIRCLE_GOVERNOR_FILE=/var/lib/warmr/circle-governor.json \
  /opt/warmr/current/venv/bin/circle-leads auto-join --driver cdp --account 4 \
  --host <slug> --screenshot-dir /var/lib/warmr/join-shots
```

`--dry-run` in place of `--host <slug>` prints what the next run would take.
The outcome goes to the database as from the timer.

The older single-page probe, `join_one.py`, prints and saves nothing; it was
on `warmr-1` only. Locally it runs against any Chrome started with
`--remote-debugging-port=9222`, from the repo root:
`python scripts/join_one.py <url>`. `join_one.py` prints one of:

| line | meaning |
|---|---|
| `RESULT joined` | membership confirmed by the API |
| `RESULT agent_needed: profile form at …` | a form; call the agent or a person |
| `RESULT agent_needed: state=unknown …` | a page the code will not guess at |
| `RESULT external_login` | the community runs its own login |
| `RESULT needs_email_code` | no code arrived within five minutes |

## What is verified, and what is not

Verified on the droplet, 2026-09-25:

- Cloudflare passed on 3 of 3 communities, twice, hours apart.
- Sign-in, the emailed 2FA code, and membership verification all completed.
- The driver files on the droplet are byte-identical to this branch.
- 197 join-related tests pass.

**Not verified. Do not assume these work:**

- **No new community has been joined from `circle-bot-prod` yet.** From
  `warmr-1` two were: entrepreneur-bootcamp by hand (2026-09-29) and hailerz
  by the timer (2026-10-01). On 2026-11-09 the carried sign-in opened four
  joined communities from `circle-bot-prod` as a member without a check, and
  the real walk (`_run_batch`) re-entered steepinsights and stored its
  session; the queue was empty, so the first new join is still to come.
- **The agent fallback is not wired on the droplet.** Hermes is not installed
  there. Today an `UNKNOWN` page stops the run and waits for a person.
- **Outcomes are not persisted** by `join_one.py` (it prints; the automatic runs above do). The three rows written
  on 2026-09-25 were written by hand in one transaction.
- **Volume from one server IP is untested.** Every run so far was a single
  visit. A series of 20–25 visits in a row from this address has not been
  attempted, and Circle rate-limits per IP.

## Troubleshooting

| symptom | cause | fix |
|---|---|---|
| `no browser on http://127.0.0.1:9222` | the service is down | `sudo systemctl restart warmr-browser`, then `journalctl -u warmr-browser -n 50` |
| Chrome exits at startup, `Aborted (core dumped)` | the userns restriction | `CHROME_DEVEL_SANDBOX` must point at a `chrome_sandbox` that is `root:root` and mode `4755` |
| the service starts and dies in a loop | unquoted `--server-args` in the unit | the `-screen 0 1280x900x24` argument must stay quoted as one word |
| state is `CLOUDFLARE` | the browser is running headless | it must run under `xvfb-run`, headed |
| `RESULT needs_email_code` | the app password, or mail landing elsewhere | check `CIRCLE_MAIL_USER` / `CIRCLE_MAIL_APP_PASSWORD`; the password must have no spaces |
| logged in but state is `LOGIN_WALL` | the async login, if the wait is ever removed | `sign_in()` must wait for the URL to leave `sign_in` |

## Accounts

The bot account is `CIRCLE_EMAIL4`, mailbox `CIRCLE_MAIL_USER`. The earlier
`zhappar01` accounts (1–3) are retired and must not be used in any new run.
