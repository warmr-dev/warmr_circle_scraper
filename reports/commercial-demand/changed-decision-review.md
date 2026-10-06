# Reviewed model comparison

This is the last completed model run, **not the final prompt**. Final prompt
adds explicit exclusions based on the six observed false positives. Its repeat
model evaluation was stopped by automatic approval review; PR remains draft.

Provider: existing Circle dashboard OpenAI credential, `gpt-4o-mini`.
Worker runtime reports the same model via `openrouter.ai`; dashboard uses direct OpenAI.
Evaluated classifier SHA-256: `b96e624d293ff1b39f4b95129029ffc3d967cdab398d676c5c35fd4eac1097a9`.

100 captured roots: baseline 1 eligible; evaluated policy 27 eligible.
28 changed outcomes: 26 newly eligible and 2 retryable processing errors.
Manual review: 20 accepted new demand decisions; 6 false positives; both errors
reviewed separately (one expected demand, one expected excluded tutorial).
No model error is counted as a semantic negative. All 9 source regression cases
matched labels and temporary-database payload eligibility in this run.

Private report includes complete source/context, old/new reasons and excerpts.
This public table uses reviewer paraphrases; it contains no private transcripts.

| Captured post ID | Old eligible | New outcome | Assessment | Reviewer rationale |
|---|---|---|---|---|
| 23016 | false | eligible | accept | Specialist customer-success hiring with enterprise adoption and renewals responsibilities. |
| 23015 | false | eligible | accept | Explicit independent SEO consultant request and concrete website/search project. |
| 23001 | false | eligible | reject | Peer networking/event attendance without a commercial provider request or concrete business problem. |
| 22777 | false | eligible | accept | Specific active executive thought-leadership publishing project; early solution exploration. |
| 22711 | false | processing_error | retry_expected_demand | Specific acquisition specialist hiring; evidence validation failed, not a semantic non-lead. |
| 22705 | false | eligible | accept | Named-tool evaluation plus concrete implementation bandwidth concern. |
| 22627 | false | eligible | accept | Exploring a CMS switch; explicitly asks for agencies and training providers. |
| 22582 | false | eligible | reject | Personal leadership-transition advice; unspecified future team gaps are not specialist hiring demand. |
| 22530 | false | eligible | accept | Explicit provider request for executive AI training sessions. |
| 22517 | false | eligible | accept | Explicit PR provider recommendation for a startup launch project. |
| 22514 | false | eligible | reject | Reports an assistant already selected and tasks/results; no remaining provider request or unresolved limitation. |
| 22508 | false | eligible | accept | Explicit agency/freelancer demand-generation audit request. |
| 22484 | false | eligible | reject | Consultant/job-seeker introduction seeking customers and networking; no buyer service request. |
| 22432 | false | eligible | accept | Concrete tool shortlist, demos, budget constraint and provider comparison. |
| 22414 | false | eligible | accept | Explicit paid fractional/contract growth partners for agency and client projects. |
| 22413 | false | eligible | accept | Named AI calling tools evaluated for a specified low-volume business use case. |
| 22394 | false | eligible | accept | Marketing director hiring with defined leadership and delivery scope. |
| 22314 | false | eligible | accept | Full captured post explicitly offers a freelance marketing contract; distinct from short negative submission. |
| 22312 | false | eligible | accept | Venue recommendation for a concrete customer/prospect event. |
| 22311 | false | eligible | reject | Peer introduction with unspecified questions; no stated commercial problem/provider need. |
| 22307 | false | eligible | reject | Solicits research survey participation; quoted reader obstacles are not author buyer demand. |
| 22275 | false | eligible | accept | Concrete company newsletter monetization/distribution exploration, constrained resources. |
| 22119 | false | eligible | accept | Explicit specialist product/PLG consulting request; rejecting agencies does not reject individual consultants. |
| 22182 | false | eligible | accept | Active product-page redesign project seeking design and information-architecture solutions. |
| 22170 | false | eligible | accept | Explicit part-time freelance webinar process support. |
| 22148 | false | eligible | accept | Specific senior marketing leadership hiring with defined responsibilities. |
| 21910 | false | eligible | accept | Explicit urgent videographer request; absence of a budget does not exclude demand. |
| 20291 | false | processing_error | retry_expected_negative | Tutorial/provider advice with an offer to review readers accounts; unsupported quote is a processing error. |
