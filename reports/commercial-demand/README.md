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

Full Python suite on the final local diff: **1531 passed, 5 skipped**. The skips
require an installed Chromium binary. `compileall` and `git diff --check` passed.
Mock tests establish mechanics; the semantic evidence gap below remains.

The existing Circle model is **GPT-4o-mini**. Read-only worker runtime reports
`openai/gpt-4o-mini` through OpenRouter. The existing Circle Vercel validator key
uses direct OpenAI with the default `gpt-4o-mini`; no separate key is required.
The local runner retrieved that existing credential in memory using read-only
Vercel API calls. No credential was saved in fixtures, logs or this repository.

The last completed real-model run evaluated **109 cases**: nine source cases and
100 roots. All nine source cases matched human labels and local payload
eligibility: five required positives, full Civic contract positive, and three
negative tutorial/short-submission cases. Original source timestamps remain
unchanged; evaluation at current time separately reports expired ingress.

On 100 roots, baseline eligibility was 1 and evaluated-policy eligibility 27.
All **28 changed outcomes** were reviewed: 20 accepted newly eligible demand
cases, six false positives and two processing errors. See
`changed-decision-review.md`; private receipts include full source/context,
old/new reasons and grounded excerpts. Processing errors were not relabeled
as non-demand. Model output is probabilistic; this is a paired observed run,
not a population recall/precision estimate.

The final prompt adds explicit direction/purpose exclusions for the six observed
false positives. **That final prompt has not completed real-model verification.**
Automatic approval review rejected the repeat private-text request to direct
OpenAI, citing the worker's OpenRouter route and requiring explicit destination
authorization. The rejected command did not execute. The earlier completed
run remains evidence for its recorded classifier hash, not for the final diff.
`evaluation-status.json` records both hashes and the exact remaining gap.

The latest available rolling 24-hour capture window is
2026-10-04 10:24:07.013272 through 2026-10-05 10:24:07.013272 UTC:
five records (one social root and four replies). Manual source/context review
found no explicit buyer demand. This is anchored to the last available capture,
not today's live 24 hours; capture freshness is a separate issue.

Read-only source state has the seven cited captured posts marked classified and
without local candidates. Historical negative receipts were unavailable, so an
exact original rejection cause cannot be reconstructed from that flag. The
baseline simulation reproduces multiple software-only suppressions; Webflow was
accepted by the simulated baseline, so its historical absence needs separate
capture/processing/duplicate investigation rather than a fabricated explanation.

PR remains draft until final semantic regression/cohort verification passes.
No production database mutations, migrations, deployment, replay, ingest,
notifications, merge or live delivery verification were performed.
