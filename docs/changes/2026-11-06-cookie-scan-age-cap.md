# Stop cookie scans at the post age cap

## Problem and change

The 2026-09-10 decision (P15, `WORKLOG.md`) was to cut posts older than
`max_post_age_days` at reading on every path. Commit `6d19d2a` did it for the
public harvest and as a triage filter, but not for cookie scans:
`scan_cookie_host` read with `since=None` on a first or post-failure read and
`last_sync_at - 7 days` otherwise, so it paged up to 100 posts per space and
fetched comments for up to 25 of them, then triage threw the old ones away.
On 2026-11-06 the cookie part of one harvest ran from 03:43 to 11:48 UTC
(`asaporg` 3 h 45 min, `siliconslopes` 1 h 48 min), and a queued "Read now"
waited behind it.

`scan_cookie_host` now reads from the later of the re-read point and
`now - max_post_age_days`. `fetch_space_posts` already stops paging at the
first page with nothing newer than `since` and fetches comments only for
posts newer than it. `max_post_age_days = 0` keeps the old behaviour. A read
held only by the cap reports `No posts newer than N day(s)` and keeps the
previous readable-space count, like a re-read.

## Scope and operations

Code only; the worker runs it, so it takes effect after a droplet deploy.
No migration. Separately, on 2026-11-06 `requirements_override.max_post_age_days`
was changed from 30 to 2 in production (see `WORKLOG.md`); Vini refuses posts
older than 48 hours. Trade-off: a new comment under a post older than the cap
is not read.

Documentation added: `README.md`
Delivery record: `docs/changes/2026-11-06-cookie-scan-age-cap.md`

## Verification and delivery

```sh
python -m pytest tests/test_cookie_scan_age_cap.py -q
# 4 passed; with the clamp removed, 2 of them fail
python -m pytest -q
# 1681 passed, 9 failed, 4 errors — the same 13 fail on clean origin/main
# in this environment (openai, playwright, launchd not installed)
```

Not measured yet: how long a harvest takes with the cap at 2 days.
