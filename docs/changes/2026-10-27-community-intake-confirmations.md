# Confirm each Warmr community intake result

## Problem and change

Warmr's community intake returns HTTP 202 even when individual payloads are
rejected. Circle previously treated every non-error HTTP response as acceptance
of the entire batch and cached all payload fingerprints. Rejected communities
could then be skipped indefinitely as already sent.

The sender now validates the complete candidate-intake receipt and caches/counts
only confirmed `candidate_created` or `existing` items. A valid partial rejection
retains successful items and retries only rejected payloads on the next ordinary
invocation. An unverifiable response confirms none of that batch. A rejected
changed/forced attempt cannot retain an older cached fingerprint. Successful
earlier batches survive later failures.

## Scope and operations

This is a sender-only protocol fix. The selected community cohort, request
payload, endpoint, secret, schedule, and import behavior are unchanged. No
historical cache reset, force-send, import, identity repair, database migration,
or production action was performed.

Documentation added: `docs/community-intake.md`
Delivery record: `docs/changes/2026-10-27-community-intake-confirmations.md`

## Verification and delivery

Offline checks cover receiver-created and existing candidates, mixed/all
rejection, malformed receipts, index correlation, unchanged-cache skips,
retry selection, forced failures, batch isolation, and Activity reporting.
The broader affected harvest and lead-export suites verify integration:

```sh
python -m pytest tests/test_community_intake.py tests/test_harvest.py tests/test_vini_ingest.py -q
# 132 passed (re-run on 2026-10-28)
git diff --check
# passed
```

Review on 2026-10-28: the current Warmr candidate-intake response contract and
local diff were inspected; CodeRabbit CLI reviewed all five changed files and
raised 0 issues. No paid review credits were used.

- Local: implemented in `codex/community-intake-confirmations`, based on
  `e960e63f7309db322a6bf8fa55415c31989c7376`.
- Committed / PR: packaged for review in `codex/community-intake-confirmations`;
  GitHub PR metadata identifies the published head.
- Merged: not done.
- Deployed/Applied: not done; no schema change required.
- Live verified: not done. Offline tests do not establish deployed behavior.
- Workspace closed: not done; isolated task worktree retained for review.

Historical false confirmations remain a separate, explicitly bounded
reconciliation task. The full partnership import remains separate work.
