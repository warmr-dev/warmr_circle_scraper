# Joins from the Mac: login, rest after a handoff, central session, location

## Problem and change

Joins moved from the droplet back to the Mac on 2026-11-06 (the droplet gets
a Turnstile checkbox on Circle's central login). Live runs from the Mac then
showed what the CDP walk (`circle_leads/join/cdp_join.py`) got wrong:

- **Continue was clicked too early.** After the email, Continue is a spinner
  while Cloudflare checks the browser; the walk gave up ("no Sign in/Continue
  control"). It now waits up to 30 s, and a Turnstile checkbox that stays is
  `challenge_stop` for a person -- nothing on it is touched.
- **The code page came straight after the email.** The central login now
  mails a code without asking for the password; the walk refilled the email.
  It now recognises the code page at every step.
- **Circle's terms box** on "Create a profile" kept Continue disabled; it is
  ticked (agreeing to terms is what `forms.py` already allows).
- **The central login counted as membership.** `login.circle.so` answers
  `/internal_api/pundit_users` for the account's own space; autohive was
  recorded as joined from its sign-in page and its cookies stored as the
  community's. Central hosts are never membership and never a session.
- **Stuck hosts were revisited every run**, each visit mailing a login code.
  A handoff now rests for 7 days (`reach.HANDOFF_REST`, `joiner.HANDOFF_COOLDOWN`);
  `--host` retries at once. The dashboard does not count a resting community
  as waiting: nine of them had made the join queue look stuck for two days.
- **A full sign-in ended the other sessions.** On 2026-11-09 six re-entries
  in a row left only the last of six sessions alive; entering through
  `login.circle.so/sign_in?request_host=<host>` with the central session in
  the browser ended none (all 11 bot sessions answered afterwards). The walk
  tries that first (`_central_entry`).
- **French "Join space"** ("Rejoindre l'espace", la-fusee) was not a Join
  control; added to both drivers' lists.
- **"Location" profile fields** (a place search) went to a person. The
  persona's `location` is now typed in and picked from Circle's suggestions;
  a place is never taken from the model.

## Scope and operations

Code; it runs on the Mac (`docs/joining.md`, "Automatic runs on the Mac"),
installed into `~/Library/Application Support/warmr/worker/venv-cdp` from this
branch on 2026-11-09 before merge (see `WORKLOG.md`). The droplet's
`warmr-join.timer` stays off. No migration; the dashboard change takes effect
on the next Vercel deploy.

Documentation added: `docs/joining.md`
Delivery record: `docs/changes/2026-11-09-join-from-the-mac.md`

## Verification and delivery

```sh
python -m pytest tests/test_cdp_join.py tests/test_join_forms.py \
  tests/test_join_flow.py tests/test_overview.py -q
```

Each new test fails without its change. Live, from the Mac with this code on
2026-11-09: hero-software joined (after a stored answer "Sonstiges" for its
"Gewerk" question), la-fusee joined through "Rejoindre l'espace", the
location helpers filled hrone's "Location" with Vietnam (not submitted:
hrone also asks a phone number), and a re-entry of joindpp through the
central session left all 11 bot sessions answering 200.
