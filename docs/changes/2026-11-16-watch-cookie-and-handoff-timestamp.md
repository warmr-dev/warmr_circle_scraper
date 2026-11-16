# Watch cookie mode, connection link, handoff timestamp

## Problem and change

Three rows were doing less than the code around them already assumed:

- **A stored session stayed on anonymous watch.** `ensure_watch_rows` and
  `watch_member_now` switched only `off` to `cookie`. A community left on
  `anon` stayed there even when `replay_sessions` held a member session. The
  home feed then went out with no cookies. One public space answers that
  request with 200, so the poller never used the session for the private
  spaces. A member session — including a paid community someone already bought
  — is now read in cookie mode from the first check and on the next refresh.
  An operator pause still forces the row off. A joined community with no
  session stays anonymous.
- **`CircleConnection.community_id` stayed null.** `connect_host` created the
  connection the worker scans, but did not point it at the community with that
  host. It now sets `community_id` when it is empty, after the watch handoff
  so a community created for a new host is included. An id already set is left
  alone. A join still points the connection at the community that was joined:
  the cookie domain can differ from that community's host, and
  `_link_connection` writes the joined id after `connect_host`.
- **A join handoff did not stamp `join_attempted_at`.** `join_status` correctly
  stays `not_attempted`, so the row remains retryable. The dashboard's week of
  rest (`reach.resting_after_handoff`) and the join queue's "last moved" both
  read `join_attempted_at`. Without the stamp those communities still counted
  as waiting, and the queue looked stuck while the bot had nothing to take.
  `_record_handoff` now sets it. The per-account daily cap still counts every
  visit from the attempt log: the timestamp is one community, not one visit,
  and it has no account on it.

## Scope and operations

Code and tests only. No migration, no configuration, no deploy. The watcher
picks up cookie mode on its next watch-list refresh (or immediately when a
session is saved). The join queue stops counting a new handoff as waiting on
the next dashboard read. Rows already handed off, with `join_attempted_at`
still empty, are unchanged until the bot visits them again.

Documentation added: `docs/joining.md`, `README.md`
Delivery record: `docs/changes/2026-11-16-watch-cookie-and-handoff-timestamp.md`

## Verification and delivery

```sh
python -m pytest tests/test_watch.py tests/test_join_flow.py tests/test_join_queue.py tests/test_session_check.py tests/test_overview.py tests/test_reach.py tests/test_replay.py -q
```
