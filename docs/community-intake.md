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
responses fail the batch closed. Earlier confirmed batches remain cached.
Harvest export Activity records the actual confirmed count and any errors.

`settings.community_intake_state` remains the existing map of source key to
payload fingerprint. An unchanged fingerprint is skipped. A changed or explicitly
forced payload requires a new confirmation; rejection or an unverifiable response
removes that attempted key's old fingerprint so it cannot remain a false success.
No new migration, endpoint, background retry loop, or receiver change is needed.

## Historical cache and rollout boundary

Legacy fingerprints may have been written without per-item confirmation. This
fix does not clear, migrate, or replay them automatically. It prevents newly
attempted rejected/unverified payloads from being recorded as delivered; it does
not attest the historical cache or restore absent Warmr records.

Do not use a global `--force` or clear the cache to validate a deployment. Prepare
an exact source-key/URL manifest, verify current target candidates, and obtain
separate approval for any bounded reconciliation. Existing invalid-slug records
such as `www.hailerz.com` remain visible errors if explicitly attempted; this
change does not alter their identities or import additional communities.

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
retry selection, forced rejection, cross-batch failure, and harvest Activity.
They make no production requests or model calls.
