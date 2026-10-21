# Offline classification acceptance - October 21, 2026

## Accepted artifact and scope

Evaluated classifier source: commit `7e3274a`, SHA-256
`b2a686a78a98f5c241ee76db6621c82138df6c35ce21b8103504cea6fc9cf38f`.
OpenRouter / `openai/gpt-4o-mini`; `ai-v3.3-grounded-direction`,
`grounded-corrective-v3`, commercial policy `commercial-demand-v1`.
Normal initial-plus-one-corrective behavior was enabled.

**39/39 passed in one same-code run, with 39 actual model calls:**

- Original incident cohort: 27/27, three demand and 24 non-leads.
- Synthetic policy-derived controls: 12/12, seven demand and five non-leads.
- Zero validation/provider errors and zero source-review disagreements.
- All 25 changed original outcomes were reviewed against complete current posts
  and available thread context. Two unchanged correct negatives were also reviewed.

Expected labels are engineering source/context review independent of database
statuses; they are not newly supplied human confirmations. Expected labels and DB
statuses were not passed to the model. Source and context hashes were checked.

## Before/after review of all original cases

Before is the earlier offline candidate `c529f32`: 22 processing errors, three
semantic mistakes and two matching negatives. This is a classifier comparison,
not a claim that production queue state has changed. Case numbers below follow
the frozen cohort order. Private IDs, authors and source text are omitted here.

| Case | Source intent | Before | Final | Review |
| --- | --- | --- | --- | --- |
| 01 | Training recommendation given | ERROR | NOT_LEAD | Pass |
| 02 | Clarification and thanks | ERROR | NOT_LEAD | Pass |
| 03 | Education/career advice given | ERROR | NOT_LEAD | Pass |
| 04 | Membership-process question | ERROR | NOT_LEAD | Pass |
| 05 | Social congratulations | ERROR | NOT_LEAD | Pass |
| 06 | Domain news | ERROR | NOT_LEAD | Pass |
| 07 | Introduction and generic feedback | ERROR | NOT_LEAD | Pass |
| 08 | Office-purchasing tool exploration | ERROR | LEAD | Pass |
| 09 | Motivational information | ERROR | NOT_LEAD | Pass |
| 10 | Tool recommendation reply | ERROR | NOT_LEAD | Pass |
| 11 | Hypothetical metric discussion | ERROR | NOT_LEAD | Pass |
| 12 | Person mention | ERROR | NOT_LEAD | Pass |
| 13 | Personal anecdote | ERROR | NOT_LEAD | Pass |
| 14 | Provider recommendation reply | LEAD | NOT_LEAD | Pass |
| 15 | Webinar recommendation | ERROR | NOT_LEAD | Pass |
| 16 | Supplier seeking client projects | ERROR | NOT_LEAD | Pass |
| 17 | Job-board guidance | ERROR | NOT_LEAD | Pass |
| 18 | Professional networking events | ERROR | NOT_LEAD | Pass |
| 19 | Community introduction | NOT_LEAD | NOT_LEAD | Pass |
| 20 | Networking-event variant | ERROR | NOT_LEAD | Pass |
| 21 | Welcome reply | ERROR | NOT_LEAD | Pass |
| 22 | Software pre-purchase research | NOT_LEAD | LEAD | Pass |
| 23 | Organizational gift research | ERROR | LEAD | Pass |
| 24 | Local professional networking | ERROR | NOT_LEAD | Pass |
| 25 | Product launch/customer discovery | ERROR | NOT_LEAD | Pass |
| 26 | Shared past experience | NOT_LEAD | NOT_LEAD | Pass |
| 27 | Agency recommendation given | LEAD | NOT_LEAD | Pass |

## Controls and pipeline verification

Real-model controls cover Webflow work, PLG/product consulting, specialist and
leadership hiring, part-time webinar support, urgent videography, tutorial
exclusion, generic office hiring exclusion, mixed supplier/buyer demand, provider
recommendations given, customer discovery, future gift purchases and hypothetical
metric discussion. They are synthetic paraphrases, not replacements for human
source labels.

Recorded accepted model verdicts were then validated through temporary SQLite
batch and triage flows using the complete frozen context: 54/54 route checks,
three candidate/payload serializations per route and 24 exclusions per route.
Socket connections and export functions were blocked. This local contract check
makes no new provider calls and does not prove live capture, export or publication.
Original source-author identity, source text and event timestamps were checked.

The older office-comparison post remains a historical demand example: at its
original capture it was within 48 hours, while it is expired at current time.
Its timestamp was not refreshed to make it deliverable. The two October 20
positives are within that age window on October 21. No posts were sent anywhere.

Full Python suite: **1648 passed, five skipped**. The five skipped checks are
local CDP browser interaction tests: Chromium is not installed. Classifier,
pipeline, storage, export and HTTP dashboard checks ran. Strict evidence verification,
description parsing, admission-rule and core validator ASTs are unchanged from
the earlier candidate. Additional direction checks reject inconsistent positive
claims; errors are not converted to negative decisions. Confidence, attribution,
freshness and deduplication contracts are retained.

## Rejected diagnostics and release boundary

Do not combine passing cases across different heads. Earlier diagnostic runs,
including the larger-evidence-window experiment and failed mixed/specialist
controls, are not acceptance evidence. The final source retains literal
model-selected quotes. Separately labelled reviewer context windows help inspect
short excerpts without automatically expanding or repairing production evidence.

No production decision writes, outbound exports, merge, production deployment,
migration, historical replay or session refresh were performed. This acceptance
is offline; natural traffic and production delivery remain unverified by design.

Private artifacts (gitignored): frozen source/context, raw normalized model
verdict receipts, source-review labels, per-case before/after review with reasons
and excerpts, local pipeline audit receipts and approval-call ledgers. The earlier
55/54 development overrun remains recorded; subsequent cycles use durable locked
budgets. The user explicitly authorized continued offline cycles until done.
