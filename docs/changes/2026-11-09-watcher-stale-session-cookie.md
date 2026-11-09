# Watcher: a re-pasted session is not shadowed by a dead copy

## Problem and change

The watcher (`circle_leads/watch/poller.py`) keeps one `requests.Session`
for its whole life. Circle answers every member request with a fresh
`_circle_session`, and the session's cookie jar keeps it. On 2026-11-06 the
sessions of `community.exitfive.com` and `community.everythingmarketplaces.com`
were pasted again at 13:10 UTC; the worker on the same droplet and a request
from the Mac read them (spaces list 200), but the watcher, up since 12:41,
sent the jar's dead `_circle_session` next to the new one, Circle took the
dead one, and every poll answered 401. *Needs attention* showed both as dead
sessions. Reproduced on the Mac: a dead `_circle_session` in the jar plus the
stored cookies gives 401, without it 200. A `systemctl restart warmr-watcher`
at 16:48 UTC cleared it (exitfive then caught up 11 posts, 1 lead).

Before a request with stored cookies (`_member_get`, `_fetch`) the watcher now
drops from its jar the cookies of that host whose names the stored session
carries. Other jar cookies (Cloudflare's `__cf_bm`) still go out.

## Scope and operations

Code; the watcher runs it, so it takes effect after a droplet deploy and a
`warmr-watcher` restart. No migration, no configuration. Until then a
re-pasted session needs `systemctl restart warmr-watcher` on `circle-bot-prod`.

Documentation added: `README.md`
Delivery record: `docs/changes/2026-11-09-watcher-stale-session-cookie.md`

## Verification and delivery

```sh
python -m pytest tests/test_watch.py -q
# 43 passed; with poller.py reverted the new test fails (unauthorized)
python -m pytest -q
# 1690 passed, 5 skipped; 1 failed + 4 errors in tests/test_launchd_installer.py,
# the same on clean origin/main (no .venv inside a fresh worktree)
```
