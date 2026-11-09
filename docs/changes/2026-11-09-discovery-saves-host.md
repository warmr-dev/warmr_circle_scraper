# Store a search find with its own address

## Problem and change

`persist_finds` (`circle_leads/discovery/persist.py`) created each new
community without `communities.host`. The column came in on 2026-09-23
(`migrations/manual/2026-09-23_p25_community_host.sql`), which filled it once
for the rows that existed then; every search find after that stayed without
one. The join-type check and the join queue both require an address
(`reach.real_host`), so none of those finds was ever checked or joined: on
2026-11-09 the Mac join bot had run 107 times since 2026-11-06 with an empty
queue, while 75 rows had a real address in `url` and no `host`.

A new find now stores the host of its link (`community_host`, the same rule
`get_or_create_community` uses; a `discover.circle.so` card stays without
one). A find whose host already belongs to another row is that row, not a new
one — `communities.host` is unique. A row found again without a host gets it,
unless another row holds that host.

## Scope and operations

Code; the worker runs discovery, so it takes effect after a droplet deploy.
No migration. Applied by hand in production on 2026-11-09 (see `WORKLOG.md`):

- `host` filled from `url` for 73 rows; 2 more skipped because another row
  already has that host (duplicates, left as they are).
- `join_type_schedule` set from the default `every_6h` to `hourly`, so a new
  community reaches the queue within about an hour.

Documentation added: `docs/joining.md`
Delivery record: `docs/changes/2026-11-09-discovery-saves-host.md`

## Verification and delivery

```sh
python -m pytest tests/test_persist.py -q
# 18 passed; with persist.py reverted, the 4 new tests fail
```
