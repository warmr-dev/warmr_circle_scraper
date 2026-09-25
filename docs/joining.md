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

DigitalOcean droplet `warmr-1`, Ubuntu 24.04.4, 3.9 GB RAM, no swap.

| thing | path |
|---|---|
| service | `/etc/systemd/system/warmr-browser.service` (enabled, `Restart=always`) |
| code + venv + Chrome | `/opt/warmr/browser-trial/` |
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

```bash
ssh warmr-1
sudo systemctl status warmr-browser          # the browser must already be up
curl -sf http://127.0.0.1:9222/json/version  # CDP answers

sudo -u warmr env $(sudo cat /etc/warmr/warmr-join.env | xargs) \
  /opt/warmr/browser-trial/venv/bin/python \
  /opt/warmr/browser-trial/join_one.py https://example.circle.so
```

It prints one of:

| line | meaning |
|---|---|
| `RESULT joined` | membership confirmed by the API |
| `RESULT agent_needed: profile form at …` | a form; call the agent or a person |
| `RESULT agent_needed: state=unknown …` | a page the code will not guess at |
| `RESULT external_login` | the community runs its own login |
| `RESULT needs_email_code` | no code arrived within five minutes |

Locally, the same thing runs against any Chrome started with
`--remote-debugging-port=9222`, from the repo root:
`python scripts/join_one.py <url>`.

## What is verified, and what is not

Verified on the droplet, 2026-09-25:

- Cloudflare passed on 3 of 3 communities, twice, hours apart.
- Sign-in, the emailed 2FA code, and membership verification all completed.
- The driver files on the droplet are byte-identical to this branch.
- 197 join-related tests pass.

**Not verified. Do not assume these work:**

- **No new community has been joined from the droplet.** The two joins on
  record were driven from the Mac; from the droplet we confirmed the login and
  the membership of communities already joined. There are 6 candidates in the
  queue, so this test is available and should be the next one run.
- **The agent fallback is not wired on the droplet.** Hermes is not installed
  there. Today an `UNKNOWN` page stops the run and waits for a person.
- **Outcomes are not persisted.** `join_one.py` prints; the three rows written
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
