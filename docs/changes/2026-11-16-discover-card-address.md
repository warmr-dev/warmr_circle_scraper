# A Discover card gets its community's address

## Problem and change

Search and the directory crawl store a Circle Discover listing as
`https://discover.circle.so/products/<slug>`, with no `communities.host`. The
join-type check and the join queue key on the host (`reach.real_host`), so no
card ever reached either. On 2026-11-16 there were 1647 such rows (291
ICP-fit). Among them a free community anyone can join: Marketing Talent
Exchange, found by search on 2026-11-12, stored as the card and priced "$33".

- The listing page carries everything needed in its server-rendered HTML: the
  Join button (an anchor around `data-testid="listing-cta…"`, else the page
  data's `external_url`) and schema.org offers with the price. New:
  `validate_finds.listing_from_html`. Checked on 23 live listings; each live
  one has the button. From the droplet the page loads without a challenge.
- A search find whose button leads to a Circle community is stored as that
  community: host, `url = https://<host>`, platform `circle`, and a note
  `directory card: found as <card>` (`persist.py`). A card stored earlier gets
  the address the next time search finds it.
- Cards already stored: `directory_cards.resolve_directory_cards`, run by the
  worker before each scheduled join-type pass (50 cards, ICP-fit first; stage
  `directory_cards`) and by hand as `circle-leads resolve-cards`. A resolved
  row has `join_type_checked_at` cleared, so the same pass asks Circle about it.
  A card leading off Circle, without a link, gone (404) or for a community
  another row holds is noted (`directory card: …`) and not read again.
- The price comes from the offers. The old dollar-sign regex took the page
  data's quoted Next.js references (`"$33"`) for amounts; it remains the
  fallback for a page without offers and now skips them.
- `discover-directory --recheck-unresolved` (browser) wrote the found link to
  `url` but no host, so even a resolved row stayed out of the queue. It now
  stores the address the same way (`apply_card_link`).

## Scope and operations

Code only; the worker runs it, so it takes effect after a droplet deploy. No
migration. At 50 cards an hour the 1647 cards are through in about a day and a
half; each costs one GET to discover.circle.so plus, for a custom domain, one
to the community (`detect_platform`).

Applied by hand before this change (see `WORKLOG.md`, 2026-11-16): row 14477
(Marketing Talent Exchange, free card) given its host and `free_join`.

Run on live pages into a local SQLite copy (2026-11-16), 9 cards: 6 resolved
(among them `www.gtmsociety.com`, free), 2 lead off Circle (The Boss Table,
SaaSRise), 1 gone (404).

Documentation added: `docs/joining.md`
Delivery record: `docs/changes/2026-11-16-discover-card-address.md`

## Verification and delivery

```sh
python -m pytest tests/test_directory_cards.py tests/test_validate_finds.py \
  tests/test_persist.py tests/test_circle_directory.py tests/test_join_type.py -q
# 120 passed. With the code reverted: the 2 new persist tests and both
# recheck tests fail, and _extract_price reads the "$33" reference as a price.
python -m pytest -q
# 1725 passed, 5 skipped; 1 failed + 4 errors in test_launchd_installer.py --
# the same as on clean origin/main in this environment (launchd).
```
