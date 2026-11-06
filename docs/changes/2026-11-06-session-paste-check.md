# Check a pasted Circle session when it is saved

## Problem and change

A session pasted in the community card (or sent by the extension) was stored
without any check. The community kept its old `session_expired` state until the
worker's next read, which can be hours away behind a long harvest. The person
pasting saw "Session saved" and the same red "session expired" next to it, and
could not tell a good cookie from a bad one (client report, 2026-11-06).

A second cause looked identical: a stored session the reading service cannot
decrypt (no `CIRCLE_CRED_KEY`, or a different one) was read as "no cookies".
The scan sent a cookieless request, got 401 and wrote `session_expired`, so a
fresh paste could never fix it.

Now:

- `POST /api/connections/{host}/session` asks Circle once
  (`scanning.verify_session`, `GET /internal_api/spaces`) before storing.
  Cookies Circle answers 401 to are refused with 422 and the stored session is
  kept. Otherwise the session is stored and the check replaces the old verdict
  on the connection: `working`; `access denied` for a 403 or for no visible
  spaces; `error` for an open profile step; `not connected` when the check got
  no clear answer. A working, unchecked or 403 session gets a priority-0 read
  queued. A 403 is not refused because the check runs from Vercel and the
  reads from the droplet: it can be the address, not the cookie. The read
  stamps (`last_sync_at`, `spaces_readable`) are not touched; a `partial`
  marker is kept.
- A working session clears the feed watcher's last `unauthorized` answer for
  the host (it was the old cookie's), so *Needs attention* drops its "needs
  verification" item at once instead of after the watcher's next pass.
- `scan_cookie_host` reads the session with `load_cookies_strict`. A session it
  cannot decrypt is an `error` naming `CIRCLE_CRED_KEY`, and no request goes to
  Circle.
- The dashboard toast and the extension popup show the check's result.

## Scope and operations

Code only. No migration, environment change, deployment or production data
change. The watcher still reads sessions with the lenient `load_cookies`.

Not covered: a "Read now" or queued read still waits behind a running harvest;
the watcher's request budget is unchanged; a worker scan already running for
the host with the old cookie can still write `session_expired` over the new
verdict when it finishes.

Documentation added: `README.md`
Delivery record: `docs/changes/2026-11-06-session-paste-check.md`

## Verification and delivery

Offline tests, no network (a conftest fixture stubs the live check for every
test; `tests/test_session_check.py` drives the real one against a fake HTTP
layer):

```sh
python -m pytest tests/test_session_check.py tests/test_member_api_reader.py tests/test_join_queue.py tests/test_replay.py -q
# 67 passed (2026-11-06)
python -m pytest -q
# 1677 passed, 9 failed, 4 errors — the same 13 fail on clean origin/main
# in this environment (openai, playwright, launchd not installed)
```

Not verified against live Circle or on the droplet.
