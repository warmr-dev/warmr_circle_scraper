# Grounded commercial-demand classification

Final offline acceptance: [39-case report](2026-10-21-validation-acceptance.md).
The original 27 incident cases and 12 policy-derived controls pass on the same
classifier source. This is not production recovery or publication evidence.

## Classification and evidence

The classifier retains independent role, ownership, direction, purpose, signal
and hiring-scope fields. The original description parser, strict evidence
validator, admission rule and confidence floor are unchanged.

The model receives the complete current post, available thread context, author
metadata and source-addressed evidence windows of up to 12 original tokens.
It selects window IDs; only known, explicitly selected IDs resolve to exact
source quotes. Unknown/missing IDs, conflicting literal quotes and context-only
evidence remain errors. There is no first-window fallback or quote repair.
Legacy literal-quote responses still undergo the original strict verifier.
Separately labelled reviewer context in the private report does not alter
model-selected evidence or production behavior.

The prompt and synthetic examples clarify:

- Own commercial problems and solution research qualify before a purchase is
  decided, including potentially free tools and future organizational purchases.
- In-house/permanent employment is not generic hiring scope: specialist and
  leadership roles qualify without an explicit third-party recruiting request.
  Ordinary nonspecialist office vacancies remain excluded.
- Recommendations given, tutorials, general feedback, networking and customer
  discovery do not transfer someone else's need to the current author.
- Suppliers can also express a distinct buyer need, including freelance work
  for themselves or an explicitly represented buyer.

Positive responses require actual JSON booleans `seeking_customers` and
`separate_buyer_need`. A positive without these facts, or with no separate buyer
need, remains a processing error. The model's factual checks are retained in the
successful description and failure diagnostics. No keyword-based source veto or
automatic conversion of errors into negative decisions is introduced.

Examples are included within the same semantic call, not in a Council or extra
adjudication stage. They apply only to the classifier prompt. The shared backend's
ICP scoring, discovery queries and form extraction retain their original message
shape. Provider credentials and model configuration are unchanged.

## Errors, retry and export

At most two model calls are allowed per evaluation: initial response plus one
corrective attempt for invalid output. Both pass the same strict checks. Repair
gets the unchanged full input, error and invalid field names; incorrect prior
label values are retained in diagnostics, not repeated as corrective anchors.

Exhausted validation stays `classified=false`, `outcome=error`,
`retry_state=needs_review`, without a scheduled loop. It is inspectable and can be
explicitly reviewed/released; it is not a completed recovery or a non-lead.
Unchanged recapture respects the hold. Provider/transport failures retain the
existing 1/5/15/60-minute retry backoff. Prior successful leads are preserved on
errors, and new candidates cannot export without a successful current decision.
No historical classification reset or policy-version backfill occurs.

Export identity, attribution, payload shape, freshness and duplicate contracts
are unchanged. The report distinguishes historical demand from current ingress
age eligibility. No migration or historical replay is included.

## Versions and reproducibility

- Classifier: `ai-v3.3-grounded-direction`.
- Validation format: `grounded-corrective-v3`.
- Commercial policy: `commercial-demand-v1`; pinned Scout reference remains
  `c4d9dfffa193aea05e1929668dec34946f79f5d5`.
- Evaluated model: OpenRouter / `openai/gpt-4o-mini`.

`scripts/evaluate_validation_cohort.py` defaults to zero provider calls, DB writes
and exports. Live evaluation requires explicit opt-in, the exact classifier SHA,
a shared approval-ledger path and an approved maximum. Each outbound attempt is
reserved under a file lock and fsynced before transmission, including provider
failures. Restarts reuse the ledger; exhaustion blocks further requests.
Results retain classifier/prompt hashes, source/context hashes, versions,
normalized verdicts and actual call counts. Private sources and receipts stay
in the gitignored incident directory. The earlier 55/54 development overrun and
rejected experiments remain recorded; they are not included as passing evidence.

## Verification boundaries

Real-model acceptance, per-case source review, local recorded-verdict pipeline
checks and the full Python suite are separate evidence layers. Five local CDP
browser tests skip because the required Chromium executable is absent. No member
session was refreshed, and no production decision, export, merge, migration or
production deployment was performed by this work. Natural traffic is unverified.
