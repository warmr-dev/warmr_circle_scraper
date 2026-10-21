# Classification and monitoring follow-up

## Frozen validation cohort

The 27 unresolved decisions at 2026-10-21 10:46:26 UTC are a finite cohort,
reconstructed from append-only classification history. One had already resolved
naturally by source export; it remains in the evaluation cohort. The private
source/context export is gitignored. These are existing processing errors, not
the 19,340 unaudited September records, which are outside this change.

PR #57's bounded corrective response keeps the exact source/author/context and
uses the original strict validator. Exhausted validation remains an error under
human review, never a negative label or a completed recovery. Provider errors
retain backoff. No bulk release, historical classification, export or replay is
performed by this change.

The local evaluator scripts/evaluate_validation_cohort.py defaults to zero model
calls and prints the cohort size. Explicit --allow-provider permits at most two
calls per supplied post, using the configured backend. It does not open a DB or
export anything. For this incident that is 27 inputs / at most 54 calls through
OpenRouter; real semantic evaluation requires explicit approval to transmit the
private current posts, author metadata and available context. Deterministic tests
verify mechanics, not real semantic recovery of all 27.

## Rhombus platform migration

Public source checks on October 21 establish:

- www.rhombus.community returns HTTP 301 to rhombus.community for the feed path.
- The target Circle feed and /internal_api/communities/current return HTML 404.
- The root returns HTTP 200 and a generator meta tag identifying Discourse.

Merely following the redirect does not restore capture. Proposed operational
change: pause the existing Circle connection/feed, mark its platform `discourse`,
record the verified canonical URL in notes, and retain its host identity,
sessions, posts, leads and watermarks. This operation has not been applied.
A Discourse reader is a separate integration; do not count Rhombus as covered.

## Attention versus history

An operator-paused member must not trigger "members but polling off" or a
polling-failure alarm. Classification error events stay in the append-only Log,
but are omitted from current Attention when that same post has a successful
verdict or a different current error. Unknown/orphaned legacy events remain
visible. Persistent validation-review holds remain visible even after 24 hours.
No acknowledgements, activity rows or failure markers are deleted.

## Verification receipt

The executable strict-validator AST matches production 2175381 exactly after
extracting it into `_validate_response` (SHA-256
baadca489c095a66dfe72c481d9e3d4872826cd11ef154f530122952f8d4d576).
Repair adds feedback and a second attempt; it does not change the acceptance
checks. Targeted pipeline, triage, commercial-demand, corrective and attention
checks: 227 passed. Real model evaluation remains unrun pending authorization.
