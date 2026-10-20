# Changed-decision review

Private source text and context were reviewed. Only anonymous reviewer paraphrases are published; the private receipt retains full reasons, excerpts and source references. Processing errors are not negative semantic decisions. Ambiguous cases have no additional human ground-truth label.

| Capture ID | Original baseline | Deployed ai-v3.1 | Candidate ai-v3.2 | Reviewer assessment |
|---|---|---|---|---|
| 23103 | NOT_LEAD | LEAD | NOT_LEAD | accepted policy decision: Professional introduction and general learning/networking; completed creative work and supplier help do not establish a distinct buyer request. |
| 23016 | NOT_LEAD | LEAD | LEAD | accepted policy decision: Specialist customer-success hiring with enterprise adoption and renewals responsibilities. |
| 23015 | NOT_LEAD | LEAD | LEAD | accepted policy decision: Explicit independent SEO consultant request and concrete website/search project. |
| 23001 | NOT_LEAD | UNCERTAIN | NOT_LEAD | accepted policy decision: Personal event attendance and peer networking; no concrete commercial event project. |
| 22996 | NOT_LEAD | NOT_LEAD | LEAD | rejected false positive: Supplier introduction and reader pain discovery; reject as a false positive if exportable. |
| 22777 | NOT_LEAD | LEAD | LEAD | accepted policy decision: Specific active executive thought-leadership publishing project; early solution exploration. |
| 22711 | NOT_LEAD | LEAD | UNCERTAIN | retryable processing error: Specific acquisition specialist hiring with defined acquisition responsibilities. |
| 22705 | NOT_LEAD | LEAD | LEAD | accepted policy decision: Named-tool evaluation plus concrete implementation bandwidth concern. |
| 22688 | NOT_LEAD | LEAD | LEAD | accepted policy decision: Concrete unresolved company marketing/reporting classification problem. |
| 22627 | NOT_LEAD | LEAD | LEAD | accepted policy decision: Exploring a CMS switch; explicitly asks for agencies and training providers. |
| 22594 | NOT_LEAD | UNCERTAIN | NOT_LEAD | accepted policy decision: Community participation habits and notification settings; social discussion without a commercial service need. |
| 22582 | NOT_LEAD | UNCERTAIN | NOT_LEAD | manual boundary review: Borderline organizational leadership advice: an inherited team and future gaps can suggest consulting; no named specialist vacancy. Requires human boundary review. |
| 22530 | NOT_LEAD | LEAD | LEAD | accepted policy decision: Explicit provider request for executive AI training sessions. |
| 22517 | NOT_LEAD | LEAD | LEAD | accepted policy decision: Explicit PR provider recommendation for a startup launch project. |
| 22514 | NOT_LEAD | NOT_LEAD | LEAD | rejected false positive: Reports a chosen assistant and completed/current tasks, inviting reader experiences; no distinct unresolved buying need. Reject as a false positive if exportable. |
| 22508 | NOT_LEAD | LEAD | LEAD | accepted policy decision: Explicit agency/freelancer demand-generation audit request. |
| 22432 | NOT_LEAD | LEAD | LEAD | accepted policy decision: Concrete tool shortlist, demos, budget constraint and provider comparison. |
| 22414 | NOT_LEAD | LEAD | LEAD | accepted policy decision: Explicit paid fractional/contract growth partners for agency and client projects. |
| 22413 | NOT_LEAD | LEAD | LEAD | accepted policy decision: Named AI calling tools evaluated for a specified low-volume business use case. |
| 22411 | NOT_LEAD | UNCERTAIN | NOT_LEAD | accepted policy decision: Prototype feedback asks whether readers would use the product, not for help with the author's buying need. |
| 22394 | NOT_LEAD | LEAD | LEAD | accepted policy decision: Marketing director hiring with defined leadership and delivery scope. |
| 22314 | NOT_LEAD | LEAD | LEAD | accepted policy decision: Full captured post explicitly offers a freelance marketing contract; distinct from short negative submission. |
| 22312 | NOT_LEAD | LEAD | LEAD | accepted policy decision: Venue recommendation for a concrete customer/prospect event. |
| 22311 | NOT_LEAD | UNCERTAIN | NOT_LEAD | accepted policy decision: Peer networking and unspecified questions without concrete demand. |
| 22307 | NOT_LEAD | NOT_LEAD | LEAD | rejected false positive: Survey participation and reader barriers are supplier/research input, not author buyer demand. |
| 22275 | NOT_LEAD | LEAD | LEAD | accepted policy decision: Concrete company newsletter monetization/distribution exploration, constrained resources. |
| 22119 | NOT_LEAD | LEAD | LEAD | accepted policy decision: Explicit specialist product/PLG consulting request; rejecting agencies does not reject individual consultants. |
| 22182 | NOT_LEAD | UNCERTAIN | LEAD | accepted policy decision: Active product-page redesign project seeking design and information-architecture solutions. |
| 22170 | NOT_LEAD | LEAD | LEAD | accepted policy decision: Explicit part-time freelance webinar process support. |
| 22155 | NOT_LEAD | UNCERTAIN | NOT_LEAD | accepted policy decision: Completed motion-design showcase inviting others to share work. |
| 22148 | NOT_LEAD | LEAD | LEAD | accepted policy decision: Specific senior marketing leadership hiring with defined responsibilities. |
| 22059 | NOT_LEAD | LEAD | NOT_LEAD | accepted policy decision: Agency promotion and prospective networking; no separate purchasing or specialist need. |
| 21910 | NOT_LEAD | LEAD | LEAD | accepted policy decision: Explicit urgent videographer request; absence of a budget does not exclude demand. |
| 20332 | NOT_LEAD | NOT_LEAD | UNCERTAIN | retryable processing error: Advertising showcase commentary, not a buyer request. |
| 20305 | NOT_LEAD | NOT_LEAD | LEAD | rejected false positive: Affiliate-program tutorial and supplier offer; the tracking problem of another participant does not belong to the root. Reject as a false positive if exportable. |
| 20249 | NOT_LEAD | LEAD | NOT_LEAD | accepted policy decision: Website tutorial and engagement question; another participant's problem cannot create buyer ownership for the root. |


## Current ai-v3.3 review - 2026-10-20

The table covers all100 root decisions; only changed cases and explicitly
reviewed prior failures have new assessments. The separate source/contrast
regressions are in acceptance-2026-10-20.json. No source bodies or literal
excerpts are published.

| Capture ID | Prior PR52 | Current candidate | Review |
|---|---|---|---|
| 23190 | NOT_LEAD | NOT_LEAD | Unchanged model decision; no new independent human label asserted. |
| 23103 | NOT_LEAD | LEAD | Rejected new false positive: professional introduction/learning/networking; no distinct buyer project. |
| 23073 | NOT_LEAD | NOT_LEAD | Unchanged model decision; no new independent human label asserted. |
| 23016 | LEAD | LEAD | Unchanged model decision; no new independent human label asserted. |
| 23015 | LEAD | LEAD | Unchanged model decision; no new independent human label asserted. |
| 23001 | NOT_LEAD | LEAD | Rejected new false positive: personal event attendance/networking, not an event-production purchase. |
| 22996 | LEAD | NOT_LEAD | Corrected prior false positive: supplier audience research and portfolio feedback. |
| 22993 | NOT_LEAD | NOT_LEAD | Unchanged model decision; no new independent human label asserted. |
| 22986 | NOT_LEAD | NOT_LEAD | Unchanged model decision; no new independent human label asserted. |
| 22985 | NOT_LEAD | NOT_LEAD | Unchanged model decision; no new independent human label asserted. |
| 22959 | NOT_LEAD | NOT_LEAD | Unchanged model decision; no new independent human label asserted. |
| 22777 | LEAD | LEAD | Unchanged model decision; no new independent human label asserted. |
| 22711 | UNCERTAIN | UNCERTAIN | Existing source hiring case still has unverified evidence; retryable, not a semantic negative. |
| 22709 | NOT_LEAD | NOT_LEAD | Unchanged model decision; no new independent human label asserted. |
| 22707 | NOT_LEAD | NOT_LEAD | Unchanged model decision; no new independent human label asserted. |
| 22705 | LEAD | LEAD | Unchanged model decision; no new independent human label asserted. |
| 22688 | LEAD | LEAD | Unchanged model decision; no new independent human label asserted. |
| 22627 | LEAD | LEAD | Unchanged model decision; no new independent human label asserted. |
| 22594 | NOT_LEAD | LEAD | Rejected new false positive: community participation/notification habits, no commercial project. |
| 22582 | NOT_LEAD | LEAD | Manual boundary review: actual team leadership/advice; do not invent a new human label. |
| 22530 | LEAD | LEAD | Unchanged model decision; no new independent human label asserted. |
| 22517 | LEAD | LEAD | Unchanged model decision; no new independent human label asserted. |
| 22514 | LEAD | NOT_LEAD | Corrected prior false positive: chosen-assistant usage, no distinct remaining buyer need. |
| 22508 | LEAD | LEAD | Unchanged model decision; no new independent human label asserted. |
| 22484 | NOT_LEAD | LEAD | Rejected new false positive: professional networking and seeking clients, not buying help. |
| 22432 | LEAD | LEAD | Unchanged model decision; no new independent human label asserted. |
| 22415 | NOT_LEAD | LEAD | Rejected new false positive: introduction and possible collaboration, no distinct buying request. |
| 22414 | LEAD | LEAD | Unchanged model decision; no new independent human label asserted. |
| 22413 | LEAD | LEAD | Unchanged model decision; no new independent human label asserted. |
| 22411 | NOT_LEAD | NOT_LEAD | Unchanged model decision; no new independent human label asserted. |
| 22394 | LEAD | LEAD | Unchanged model decision; no new independent human label asserted. |
| 22359 | NOT_LEAD | NOT_LEAD | Unchanged model decision; no new independent human label asserted. |
| 22358 | NOT_LEAD | UNCERTAIN | Retryable semantic contradiction on supplier copywriting offer; not a correct negative. |
| 22321 | NOT_LEAD | NOT_LEAD | Unchanged model decision; no new independent human label asserted. |
| 22314 | LEAD | UNCERTAIN | Retryable unverified quote on a genuine freelance contract; separate source fixture passes but this metadata/context run does not. |
| 22312 | LEAD | LEAD | Unchanged model decision; no new independent human label asserted. |
| 22311 | NOT_LEAD | NOT_LEAD | Unchanged model decision; no new independent human label asserted. |
| 22309 | NOT_LEAD | NOT_LEAD | Unchanged model decision; no new independent human label asserted. |
| 22307 | LEAD | LEAD | Prior false positive persists: survey/research participation. |
| 22302 | NOT_LEAD | NOT_LEAD | Unchanged model decision; no new independent human label asserted. |
| 22276 | NOT_LEAD | NOT_LEAD | Unchanged model decision; no new independent human label asserted. |
| 22275 | LEAD | LEAD | Unchanged model decision; no new independent human label asserted. |
| 22119 | LEAD | LEAD | Unchanged model decision; no new independent human label asserted. |
| 22182 | LEAD | NOT_LEAD | Rejected new false negative: a concrete product-page redesign asks for references/design/architecture. |
| 22170 | LEAD | LEAD | Unchanged model decision; no new independent human label asserted. |
| 22165 | LEAD | LEAD | Unchanged model decision; no new independent human label asserted. |
| 22155 | NOT_LEAD | NOT_LEAD | Unchanged model decision; no new independent human label asserted. |
| 22148 | LEAD | LEAD | Unchanged model decision; no new independent human label asserted. |
| 22135 | NOT_LEAD | NOT_LEAD | Unchanged model decision; no new independent human label asserted. |
| 22132 | NOT_LEAD | NOT_LEAD | Unchanged model decision; no new independent human label asserted. |
| 22130 | NOT_LEAD | NOT_LEAD | Unchanged model decision; no new independent human label asserted. |
| 22101 | NOT_LEAD | NOT_LEAD | Unchanged model decision; no new independent human label asserted. |
| 22059 | NOT_LEAD | LEAD | Rejected new false positive: agency introduction offering services and networking. |
| 21981 | NOT_LEAD | NOT_LEAD | Unchanged model decision; no new independent human label asserted. |
| 21910 | LEAD | LEAD | Unchanged model decision; no new independent human label asserted. |
| 20344 | NOT_LEAD | NOT_LEAD | Unchanged model decision; no new independent human label asserted. |
| 20340 | NOT_LEAD | NOT_LEAD | Unchanged model decision; no new independent human label asserted. |
| 20338 | NOT_LEAD | NOT_LEAD | Unchanged model decision; no new independent human label asserted. |
| 20332 | UNCERTAIN | NOT_LEAD | Correctly resolved prior processing error: advertising commentary/showcase is not a buyer request. |
| 20330 | NOT_LEAD | NOT_LEAD | Unchanged model decision; no new independent human label asserted. |
| 20326 | NOT_LEAD | NOT_LEAD | Unchanged model decision; no new independent human label asserted. |
| 20324 | NOT_LEAD | NOT_LEAD | Unchanged model decision; no new independent human label asserted. |
| 20321 | NOT_LEAD | NOT_LEAD | Unchanged model decision; no new independent human label asserted. |
| 20316 | NOT_LEAD | NOT_LEAD | Unchanged model decision; no new independent human label asserted. |
| 20313 | NOT_LEAD | NOT_LEAD | Unchanged model decision; no new independent human label asserted. |
| 20312 | NOT_LEAD | NOT_LEAD | Unchanged model decision; no new independent human label asserted. |
| 20308 | NOT_LEAD | LEAD | Rejected new false positive: instructional advertising tips and an offer to audit readers accounts. |
| 20305 | LEAD | LEAD | Prior false positive persists: affiliate tutorial/supplier offer. |
| 20302 | NOT_LEAD | LEAD | Rejected new false positive: advertising tutorial and supplier free-audit offer. |
| 20301 | NOT_LEAD | LEAD | Rejected new false positive: advertising tutorial and supplier free-audit offer. |
| 20300 | NOT_LEAD | NOT_LEAD | Unchanged model decision; no new independent human label asserted. |
| 20298 | NOT_LEAD | LEAD | Rejected new false positive: advertising tips and offering assistance to readers. |
| 20296 | NOT_LEAD | NOT_LEAD | Unchanged model decision; no new independent human label asserted. |
| 20291 | NOT_LEAD | LEAD | Rejected new false positive: instructional recommendations and supplier audit offer. |
| 20289 | NOT_LEAD | NOT_LEAD | Unchanged model decision; no new independent human label asserted. |
| 20287 | NOT_LEAD | NOT_LEAD | Unchanged model decision; no new independent human label asserted. |
| 20284 | NOT_LEAD | NOT_LEAD | Unchanged model decision; no new independent human label asserted. |
| 20282 | NOT_LEAD | NOT_LEAD | Unchanged model decision; no new independent human label asserted. |
| 20278 | NOT_LEAD | NOT_LEAD | Unchanged model decision; no new independent human label asserted. |
| 20273 | NOT_LEAD | UNCERTAIN | Retryable semantic contradiction on a tips/engagement post; not a correct negative. |
| 20268 | NOT_LEAD | NOT_LEAD | Unchanged model decision; no new independent human label asserted. |
| 20256 | NOT_LEAD | NOT_LEAD | Unchanged model decision; no new independent human label asserted. |
| 20254 | NOT_LEAD | NOT_LEAD | Unchanged model decision; no new independent human label asserted. |
| 20252 | NOT_LEAD | NOT_LEAD | Unchanged model decision; no new independent human label asserted. |
| 20249 | NOT_LEAD | LEAD | Rejected new false positive: tutorial engagement question, not the authors own project. |
| 20244 | NOT_LEAD | NOT_LEAD | Unchanged model decision; no new independent human label asserted. |
| 20243 | NOT_LEAD | NOT_LEAD | Unchanged model decision; no new independent human label asserted. |
| 20242 | NOT_LEAD | NOT_LEAD | Unchanged model decision; no new independent human label asserted. |
| 20241 | NOT_LEAD | NOT_LEAD | Unchanged model decision; no new independent human label asserted. |
| 20240 | NOT_LEAD | NOT_LEAD | Unchanged model decision; no new independent human label asserted. |
| 20239 | NOT_LEAD | NOT_LEAD | Unchanged model decision; no new independent human label asserted. |
| 20238 | NOT_LEAD | NOT_LEAD | Unchanged model decision; no new independent human label asserted. |
| 20237 | NOT_LEAD | NOT_LEAD | Unchanged model decision; no new independent human label asserted. |
| 20236 | NOT_LEAD | NOT_LEAD | Unchanged model decision; no new independent human label asserted. |
| 20235 | NOT_LEAD | NOT_LEAD | Unchanged model decision; no new independent human label asserted. |
| 20234 | NOT_LEAD | NOT_LEAD | Unchanged model decision; no new independent human label asserted. |
| 20233 | NOT_LEAD | NOT_LEAD | Unchanged model decision; no new independent human label asserted. |
| 20232 | NOT_LEAD | NOT_LEAD | Unchanged model decision; no new independent human label asserted. |
| 20231 | NOT_LEAD | NOT_LEAD | Unchanged model decision; no new independent human label asserted. |
| 20230 | NOT_LEAD | NOT_LEAD | Unchanged model decision; no new independent human label asserted. |
