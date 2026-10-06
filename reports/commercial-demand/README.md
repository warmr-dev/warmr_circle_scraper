# Acceptance evidence (local implementation; draft PR)

Baseline Circle: `0268e44670804036cb5faa3a367c5a04bdb960da`.
Scout reference: `c4d9dfffa193aea05e1929668dec34946f79f5d5`;
`classifier.py` and `council.py` verified against available GitHub HEAD and local
reference files. Circle adopts the commercial policy with one semantic call.

## What changes

| Stage | Baseline | New behavior |
|---|---|---|
| Keyword prefilters | Negation, seeker and low score can terminate before LLM | Diagnostic signals; mixed posts reach semantic evaluation |
| Semantic rule | Software work plus restricted engagement description | Buyer with an explicit commercial demand signal in any category |
| Text/context | Root excerpt truncated at 6,000 characters | Complete current post plus available root/replies; inspectable input-limit error |
| Admission | Batch roles/skills gating differs from triage | Shared evaluation and confidence admission; score/category only prioritize |
| Provider failure | Rules fallback, held candidate, classified terminally | Durable error, no new candidate, no retirement, bounded retry |
| Negative receipt | No durable post audit; logging depends on path/config | JSON audit and transactional append-only activity receipt |
| Export | Candidate/legacy state governs retry drain | New audit must be successful/current/eligible; legacy contract preserved |

## Source and cohort provenance

`cohort-manifest.json` pins 100 unique Exit Five root posts, IDs, capture times
and content/context hashes. Capture boundary: 2026-09-28 through 2026-10-05 UTC.
The complete source snapshot and replies were retrieved read-only in small batches.
Private transcripts, source fixtures and raw provider responses remain local in
`private/` and gitignored `decisions/` because this GitHub repository is public.
Public CI uses synthetic paraphrases; local regression tests additionally use
all nine retrieved source cases and their explicit human labels.

The five requested positives must reach candidate creation and payload
serialization with original source times at original capture. AI tutorial and
short Civic Shout submissions remain negatives. The full Civic Shout capture is
separately positive (freelance contract); this is the user-approved distinction.
The tutorial's full capture is also tested as a negative.

## Verification status

Local full Python suite: **1523 passed, 5 skipped** (browser integration tests
require an installed Chromium binary). After the last backend-selection change,
**87 relevant tests passed**. `compileall` and `git diff --check` passed.

Mock regression tests establish pipeline mechanics and export serialization,
including original timestamps, 48-hour freshness at capture and expired ingress
at today's time. They do not establish model semantic accuracy.

Real-model old/new comparison was attempted after explicit user authorization
of private-content disclosure to Anthropic. The run was interrupted on repeated
HTTP 401 authentication failures. Both locally available primary and fallback
keys also failed the read-only models endpoint. There are **zero successful
model responses**; 68 cached cases are processing failures, not semantic labels.
`evaluation-status.json` records the aggregate evidence without private content.

The runner now checks provider credentials before scheduling cohort inference
and supports `--retry-errors`, retaining prior error receipts. Authorization is
resolved; a valid Anthropic credential is the remaining execution dependency.

With valid credentials, run `scripts/evaluate_commercial_demand.py` with the private bundle,
using `--retry-errors`, record all regression results, separate model errors, and review **every** changed
cohort decision. Write old/new outcome, grounded excerpts and reviewer assessment.
Until that evidence exists, this PR remains draft and is not acceptance-complete.

No production database mutations, migrations, deployment, replay, ingest,
notifications, merge or live delivery verification were performed.
