# Community intake confirmations

The existing sender registers selected monitored Circle communities with Warmr
through `POST /api/external/communities`. The selection remains watched or
approved Circle rows plus unpaused private connections with stored sessions.
It runs after harvest, or through the existing `push-communities` command.
This is not a full-catalog import or a synchronization of membership and scores.

## Acceptance and cache

An HTTP 2xx response alone does not confirm delivery. Warmr can return HTTP 202
with per-item errors. The sender requires the current candidate-intake response:

- `ok: true`, integer `received`, `accepted`, `existing`, and `rejected` counts;
- one result for every request item, each with a unique, in-range `item_index`;
- `candidate_created`, `existing`, or `error` status, consistent with the counts;
- a candidate UUID and matching normalized name for each confirmed item. Name
  normalization follows Warmr's lowercase ASCII alphanumeric candidate key.

The `sent` count includes only `candidate_created` and `existing` results that
passed these checks. A complete mixed response caches only those items; errors
remain pending for a later ordinary invocation. Reordered results are matched
by `item_index`, never by response order.

Invalid JSON, missing results, duplicate or invalid indices, unknown statuses,
missing candidate IDs, name mismatches, inconsistent counts, and non-2xx HTTP
responses confirm nothing from that POST. Earlier confirmed batches remain
cached. When the portal refuses a multi-item body (HTTP 4xx, or a receipt that
does not validate), the same communities are sent again one at a time, so one
bad item does not zero the chunk. A 5xx or a connection error stays one batch
failure and is not multiplied. Harvest export Activity records the actual
confirmed count and any errors.

## Slug sent to the portal

The portal accepts a slug of lowercase letters, digits, and hyphens. It rejects
a host (`slug must use lowercase letters, digits, and hyphens`). Custom-domain
rows in this database keep the raw host as `communities.slug` (that value is
the row's key). The intake payload does not send that key. It sends
`community_slug_for_host` of the host:

| host | slug sent |
|---|---|
| `community.everythingmarketplaces.com` | `everythingmarketplaces` |
| `community.exitfive.com` | `exitfive` |
| `community.steepinsights.com` | `steepinsights` |
| `www.firefly-innovations.org` | `firefly-innovations` |
| `www.startup-ready.com` | `startup-ready` |
| `www.hailerz.com` | `hailerz` |
| `<slug>.circle.so` | `<slug>` |

A label that is still illegal after that (an underscore, for example) is left
out of the POST and reported as an error. It is not cached as delivered, and
it does not ride in a chunk with communities the portal would accept. The
database slug is not rewritten.

`settings.community_intake_state` remains the existing map of source key to
payload fingerprint. An unchanged fingerprint is skipped. A changed or explicitly
forced payload requires a new confirmation; rejection or an unverifiable response
removes that attempted key's old fingerprint so it cannot remain a false success.
No new migration, endpoint, background retry loop, or receiver change is needed.

## Historical cache and rollout boundary

Legacy fingerprints may have been written without per-item confirmation. This
sender does not clear, migrate, or replay the cache by itself, and this change
does not write to the production database.

A fingerprint is the hash of the payload, including `slug`. Communities that
were cached with the raw host as `slug` (including `www.hailerz.com`) no longer
match. The next ordinary intake, harvest or `push-communities` without
`--force`, sends them again with the registrable label. That is the resend.
Do not clear `settings.community_intake_state` and do not pass `--force` to
validate a deploy: a global force would resend every monitored community, not
only the ones whose slug changed.

The cache key stays `community:<stored slug>` or `connection:<host>`. The
portal may already hold a rejected candidate under the dotted slug. The resend
registers the hyphenated slug as its own candidate. It does not rename or
delete the old portal row. Prepare an exact source-key/URL manifest and obtain
separate approval before any cleanup of those old portal rows.

A changed or rejected attempt still drops that key's old fingerprint, so a
previously cached dotted slug is not left looking delivered after a failed
resend. Communities that leave the monitored set are still forgotten from the
cache only. No new migration, endpoint, or background retry loop is added.

Deploy through the existing Circle release procedure only after the normal
release decision. Observe the next naturally due export using the per-item
results, Activity counts, and actual Warmr candidate readback. A historical
fingerprint or a skipped run does not establish live acceptance of the fix.

## Offline validation

```sh
python -m pytest tests/test_community_intake.py tests/test_harvest.py tests/test_vini_ingest.py -q
git diff --check
```

Tests use isolated SQLite databases and mocked HTTP responses. They exercise
full success, existing candidates, mixed/all rejection, malformed receipts,
retry selection, forced rejection, cross-batch failure, harvest Activity,
registrable-label slugs (including a cached dotted slug resent as `hailerz`),
an illegal slug held out of the POST, and a rejected batch retried one
community at a time. They make no production requests or model calls.
