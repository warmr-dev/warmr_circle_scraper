# Grounded evidence and buyer-intent follow-up

The first frozen-cohort evaluation did not recover the incident: 22 invalid
responses remained and three mechanically valid outcomes disagreed with source
review. Later development runs are diagnostics, not final cohort acceptance.

## Candidate behavior

- Keep the existing independent author-role, demand-owner, direction, purpose,
  demand-signal and hiring-scope description. Do not derive missing intent fields
  from a single permissive label. The commercial rule, confidence floor, source
  attribution and strict validator are unchanged.
- Offer source-addressed evidence spans alongside the complete original current
  post, available context and author metadata. Spans are contiguous windows of
  up to 12 whitespace-delimited tokens, with original source, offsets and text.
  No spelling, case, punctuation or whitespace is corrected in these slices.
- The model selects evidence IDs. Only explicitly selected known IDs resolve to
  exact original quotes. Unknown/missing IDs, contradictory literal quotes and
  context-only evidence remain errors. There is no first-span fallback and no
  repair of an ungrounded model quote. Legacy literal quotes must still pass the
  original evidence validator.
- Shorten the policy instructions and add synthetic multi-turn examples to the
  same model request. Examples distinguish recommendations given from sought,
  supplier/customer discovery from buying, conceptual practice from an actual
  unresolved business problem, and networking from commercial procurement.
  Physical goods and future purchase exploration still qualify.
- The examples contain no incident IDs, private authors or community names.
  They add no model calls and no Council. All supported backends receive the
  same examples. Actual provider and model are unchanged.
- Preserve existing two-call correction bound, provider retry behavior, durable
  review holds, candidate/export contracts and freshness/deduplication gates.
  Adding evidence spans counts toward the explicit context limit; excess input
  remains an inspectable processing error, never silent truncation/non-demand.

Classifier version: `ai-v3.3-grounded-direction`; validation format:
`grounded-corrective-v3`; commercial policy remains `commercial-demand-v1`.
No automatic reclassification, historical replay, migration or deployment.

## Verification and evaluation identity

The evaluator now requires the expected SHA-256 of the reviewed classifier file
before constructing the provider client. Results record that SHA, prompt SHA,
policy/classifier/validation versions and actual model-call count. This prevents
a delayed upload from silently evaluating an older temporary candidate.

Full source and diagnostic outputs remain private/gitignored. Engineering labels
are source-review assessments, not new human ground truth. A final, same-head
27-case model evaluation is still required; this document does not assert that
all cases pass or that production errors are cleared.

The development approval was exceeded by one request: 55 calls versus its
54-call ceiling. Calls were stopped. The evaluator now reserves each outbound
attempt in a file-locked, fsynced approval ledger before transmission, including
provider failures; restarts reuse the same ledger. An exhausted ledger blocks
additional requests. The final cohort run needs a new explicit approval and a
fresh approval ledger; previous diagnostic outputs are not final acceptance.

Local verification: the strict `_validate_response`, `verify_evidence`,
`read_description` and `is_lead` function ASTs match the pre-change version.
Mechanical tests cover missing/unknown evidence IDs, contradictory literal
quotes, current-source ownership, full-context preservation, demand at the end
of long posts, persistent call budgets, and both batch/triage export eligibility.
These tests do not prove semantic correctness of model decisions.

Final local suite: **1615 passed, 5 skipped**. `git diff --check` is clean.
This is mechanical verification; final same-candidate real-model acceptance
remains pending and release is blocked.


## Commercial-direction consistency follow-up

The first SHA-verified run on 9e0340f used 27 calls: all 27 responses passed the
strict evidence/schema validator and 26 matched source review. One product-launch
post seeking introductions to prospective customers was still called buyer demand.
This is a semantic failure, not a provider or quote-validation failure.

The follow-up requires two independent factual checks for every positive response:
`seeking_customers` and `separate_buyer_need`, both actual JSON booleans. A positive
without these facts, or with `separate_buyer_need=false`, is a validation error
that remains non-exportable. It is never silently converted to NOT_LEAD.
A supplier with a distinct expressed buyer need remains eligible. The model still
reads the complete source and context; there is no phrase/keyword suppression.
Checks are persisted with the positive description and on error diagnostics.
The original strict validator and commercial rule remain unchanged; this is an
additional consistency requirement. The normal two-call correction bound stays.

A final full-cohort single-attempt check is proposed within the remaining 27
calls of the same 54-call approval. It will reuse the shared ledger; no extra
budget, production decisions, exports, merge or deployment are authorized.


## Completed 54-call approval and remaining acceptance gap

The approval was consumed exactly, without another overrun:

| Evaluated head | Inputs | Calls | Matches source review | Remaining failures |
| --- | ---: | ---: | ---: | --- |
| 9e0340f | 27 | 27 | 26 | One customer-discovery false positive |
| b8c1275 | 27 | 27 | 25 | One gift-purchase false negative; one inconsistent supplier response |

The second run intentionally allowed one attempt per input to stay within the
remaining 27-call budget. It did not prove the normal corrective second attempt
for the supplier error. Do not combine successes across these heads and claim a
passing cohort. Both complete input/context hashes were verified; detailed
private results retain source-review assessments and response diagnostics.

The current candidate clarifies that organizational gift/budget exploration is
buying research and includes a synthetic example. Supplier-only descriptions
must use consistent supplier/none/offer fields. Corrective feedback now provides
the error and invalid field names rather than repeating the previous incorrect
intent labels; those labels remain available in local audit diagnostics. Full
source/context and the original strict validator are preserved for both calls.

A fresh full-cohort evaluation with the normal two-call bound requires a new
explicit provider-call approval. No production decision has been written, no
outbound export was made, and release acceptance remains blocked.


## Supporting-evidence review

The normal two-attempt run on 54b10b1 matched all 27 source-reviewed outcomes
(three demand, 24 excluded) using 27 calls. Local recorded-verdict checks passed
all 54 batch/triage routes, with three candidates per route and no outbound calls.
This is frozen-cohort/local evidence, not production recovery or publication.

Review found that the model often picked the first short source window, sometimes
cutting a request before its object. Windows now hold up to 48 original tokens;
instructions require the actual request/offer rather than only a heading, and a
synthetic long-introduction example selects the final demand span. Source text,
context, strict validation and admission criteria remain unchanged. The final
candidate must be reevaluated before release acceptance is claimed.


## Final source selection

The larger-window/extra-example experiment on 40faf90 regressed a motivational
post into demand and is not accepted. The final classifier source is restored
byte-for-byte to the accepted 54b10b1 implementation (SHA-256
f08d28fcfc02fb45af057ebac1f154120e339191289bcbc9d5e1d20ad70fd341).
Do not combine outputs from the rejected experiment into acceptance evidence.

Full-source review windows are supplied in the private per-case review report,
separately labelled as reviewer context, not model-selected evidence. Selected
model quotes stay untouched and pass the strict verifier; source references link
the complete post/context. No automatic quote expansion or quote repair is added
to production behavior. The 12 synthetic boundary paraphrases remain regression
fixtures, independent of private source labels.


## Mixed supplier/buyer control

On the accepted 54b10b1 source, 11/12 synthetic controls matched expected labels.
The mixed case (a supplier seeking customers and separately hiring a freelancer)
returned an inconsistent model description. It remained a processing error, not
a negative or exported candidate. A complete synthetic mixed-role example now
shows buyer ownership, mixed purpose/direction and the separate real hiring need.
The admission rule and strict validator are unchanged; final acceptance requires
both the original 27 and all 12 controls on this same candidate.


## Work-task research control

The d953a81 run matched 38/39 source/control labels. The office price-comparison
question was incorrectly excluded for lacking direct purchase intent. The prompt
now explicitly defines buyer role to include solution research for a concrete
commercial task, including potentially free tools and unnamed options. It forbids
using the absence of direct buying/hiring as the exclusion reason for such a task.
Synthetic office-tool research and audience-directed informational advice show
both sides of the boundary. Separate buyer need also includes an explicitly
represented buyer, consistent with the existing admission rule.


## Specialist scope control

The 5e9cf62 run matched the original 27 and 11/12 controls. The specialist and
leadership hiring control was incorrectly tagged generic in-house. Scope wording
now distinguishes employment mode from specialization: permanent internal
leadership/professional roles qualify without an explicit staffing-agency request;
ordinary nonspecialist office staffing stays excluded. Paired synthetic examples
show both cases. This restores the reference policy rather than broadening its
exclusions or accepting unsupported evidence.
