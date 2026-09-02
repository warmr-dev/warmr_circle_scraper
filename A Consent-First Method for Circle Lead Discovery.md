# A Consent-First Method for Circle Lead Discovery

## Executive Summary

- **No Cookie-Based Scraper**: Circle's terms prohibit third-party applications that interact with the service without prior written consent, explicitly including scripts that scrape or extract data [executive_summary[0]] [6] -> do not automate your account's browser session, copy cookies, or call undocumented endpoints.
- **Member Account Is Not An API Credential**: Headless access requires a `Headless Auth` token created by each community's admin, followed by a community-scoped member JWT [executive_summary[1]] [11] -> your account can be the authorized member, but the operator still must enable the integration.
- **Discover Is Only A Starting List**: Circle Discover publishes community descriptions and prices, including some offers labeled `Free` [executive_summary[2]] [10] -> use it to identify candidates, not as evidence that their conversations may be harvested.
- **Posts And Chats Need Different Paths**: Admin API v2 can read spaces, posts, comments, and members [executive_summary[3]] [16], while the Headless Member API exposes member-visible posts and chat-room messages [executive_summary[4]] [15] -> select the integration by content type.
- **Direct Messages Should Stay Excluded**: The documented Member API reference exposes chat rooms and chat threads but does not list a separate direct-message export endpoint [executive_summary[4]] [15] -> collect only operator-approved communal spaces, not DMs.
- **Incremental Collection Beats Re-Scraping**: The Data API supports pagination links and `created_at_gt` incremental synchronization [executive_summary[5]] [14] -> for partner communities with the appropriate product access, ingest only new events.
- **Lead Detection Needs Human Review**: Statements such as "looking for a developer" can be requests, self-promotion, jokes, or quotations -> score candidates, retain the permalink and context, and require a person to approve outreach.
- **Community Rules Control Outreach**: Circle's terms also prohibit impersonation, unauthorized access, and obtaining information through means not intentionally provided [executive_summary[0]] [6] -> contact a lead only through a channel and in a manner permitted by the operator and member.

## 1. What Your Circle Account Does And Does Not Authorize

I would not build the requested system as a browser scraper using your Circle login. Circle's Platform Terms, last updated December 1, 2025 [1_what_your_circle_account_does_and_does_not_authorize[0]] [6], expressly prohibit unapproved third-party applications, including scripts designed to scrape or extract data [1_what_your_circle_account_does_and_does_not_authorize[0]] [6]. The fact that Circle's robots file permits search and AI input [1_what_your_circle_account_does_and_does_not_authorize[1]] [17] does not override that account-level contractual restriction. Robots directives describe crawler preferences; they are not authorization to collect logged-in member conversations.

Joining a free community gives your account the access chosen by that community. It does not turn the account into an administrator, create a bulk-export right, or waive the community's rules. Space-level controls still determine access: Circle states that access depends on the space configuration and membership, and a non-member opening a private space can receive a lock screen even when the URL is known [1_what_your_circle_account_does_and_does_not_authorize[2]] [5].

Use this authorization matrix:

| Activity | Acceptable method | Required authority | Default decision |
|---|---|---|---|
| Find candidate communities | Manually review Circle Discover and community landing pages | Publicly displayed information | Allowed for discovery |
| Join a free community | Complete its normal signup manually and truthfully | Community's signup rules | Allowed, but not ingestion consent |
| Read content personally | Circle UI under your genuine account | Access granted to that account | Allowed subject to community rules |
| Bulk-read posts | Admin API v2 or an approved read-only integration | Community operator-issued token or app approval | Preferred ingestion path |
| Bulk-read chat rooms | Headless Member API with a community-issued Headless Auth token and member JWT | Community operator cooperation | Allowed only for approved rooms |
| Scrape authenticated HTML | Browser automation, copied cookies, or internal APIs | Circle's prior written consent plus operator authorization | Do not implement without both |
| Collect DMs | Private-message harvesting | Clear participant consent and a documented supported route | Exclude by default |

Circle's own architecture is the key case study: it deliberately separates admin automation from member-side experiences. Admin API tokens are created by community admins, while Headless calls are performed on behalf of a specific signed-in member [1_what_your_circle_account_does_and_does_not_authorize[3]] [3][1_what_your_circle_account_does_and_does_not_authorize[4]] [4]. That separation means one personal account is not designed to become a cross-community extraction token.

## 2. Discover And Enroll Without Automating Accounts

Start with a manually maintained `communities` registry. Circle Discover is an official marketplace where members find communities and courses [2_discover_and_enroll_without_automating_accounts[0]] [18], and its listings show descriptions and prices, including at least one community marked `Free` [2_discover_and_enroll_without_automating_accounts[1]] [10]. Capture only public listing metadata at this stage:

- Community name and landing-page URL
- Topic and likely buyer profile
- Price shown, such as `Free`
- Signup method
- Operator contact
- Public community rules
- Whether an API or analytics partnership is available
- Permission status: `candidate`, `contacted`, `approved`, `denied`, or `revoked`

Enroll manually using your real identity. Do not automate signup forms, invitations, email verification, SSO, MFA, CAPTCHAs, acceptance questions, or access-group selection. Do not join under a misleading persona; Circle's terms expressly prohibit impersonation and inaccurate submissions [2_discover_and_enroll_without_automating_accounts[2]] [6].

After joining, contact the operator with a narrowly defined proposal. A suitable request is:

> I run a tool that identifies posts in which members explicitly request software-development help. May I process posts from the following spaces for this purpose? The system will exclude DMs and private member profiles, retain content for 30 days, show candidates to a human reviewer, and follow your outreach rules. I can use a read-only Circle API token or member-scoped Headless integration that you control. You can revoke access and request deletion at any time.

Record the response and exact allowed spaces. Because Circle processes member information on behalf of each Creator, and the Creator may be the controller or business for that information [2_discover_and_enroll_without_automating_accounts[3]] [8], operator participation is not merely a courtesy. It is central to defining a legitimate, community-specific workflow. For HTML scraping specifically, obtain Circle's prior written consent as well; operator permission alone does not erase the platform's explicit terms.

## 3. Choose The Official Ingestion Path

Use the least powerful route that meets the approved purpose.

| Route | Suitable content | Authentication | Important limitation |
|---|---|---|---|
| Admin API v2 | Community, spaces, posts, comments, and members | Admin token from `Developers -> Tokens` [3_choose_the_official_ingestion_path[0]] [3] | The supplied v2 reference lists no GET endpoint for chat messages [3_choose_the_official_ingestion_path[1]] [16] |
| Headless Member API | Member-visible spaces, posts, comments, home feed, chat rooms, and chat-room messages | Community-created Headless Auth token, then member JWT [3_choose_the_official_ingestion_path[2]] [11] | Available through Circle's Headless offering on Business plans and above [3_choose_the_official_ingestion_path[3]] [4] |
| Data API | Warehouse-oriented chat and post event streams | Community-owned Data API connection | Confirm that the event payload contains the text fields required; the visible reference establishes event models but not every payload field [3_choose_the_official_ingestion_path[4]] [14] |
| Circle MCP, read-only | Ad hoc AI-assisted review of community resources | OAuth connection and app/workspace approval | Better for bounded human workflows than bulk ETL |
| Authenticated HTML | UI content | Account cookies or session | Not recommended; requires Circle's prior written consent for scraping [3_choose_the_official_ingestion_path[5]] [6] |

### Path A: posts through Admin API v2

This is the cleanest route when the operator wants organization-wide post analysis. Circle recommends v2 because new endpoints and updates go there [3_choose_the_official_ingestion_path[0]] [3]. The reference includes GET operations for spaces, posts, comments, and community members [3_choose_the_official_ingestion_path[1]] [16].

Use `page`, `per_page`, and the response's `has_next_page` rather than guessing the last page. Circle documents a default page of 1, default `per_page` of 10, and warns that counts can change while posts are added or removed [3_choose_the_official_ingestion_path[0]] [3]. Store source IDs and upsert records so changing totals do not create duplicates.

### Path B: chats through Headless Member API

For approved communal chat rooms, the operator creates a `Headless Auth` token. Your Circle account is identified by `community_member_id`, email, or SSO ID in a POST to `/api/v1/headless/auth_token`; Circle returns an access JWT and refresh token [3_choose_the_official_ingestion_path[2]] [11]. The JWT is community-scoped and identifies both `community_member_id` and `community_id` [3_choose_the_official_ingestion_path[2]] [11].

Use the member JWT with these documented GET routes:

- `/api/headless/v1/spaces`
- `/api/headless/v1/spaces/{space_id}/posts`
- `/api/headless/v1/posts/{post_id}/comments`
- `/api/headless/v1/messages`
- `/api/headless/v1/messages/{chat_room_uuid}/chat_room_messages`

These routes are documented in the Member API reference [3_choose_the_official_ingestion_path[6]] [15]. Only traverse operator-approved room and space IDs. Do not assume that chat-room access implies permission to process every message for sales purposes.

### Path C: Data API for ongoing partner communities

For operators already using Circle's Data API, request a restricted warehouse feed. Responses contain `events` and `pagination`, expose `next_page_uri`, and support `created_at_gt` based on the latest synchronized event [3_choose_the_official_ingestion_path[4]] [14]. This is preferable to repeatedly downloading the entire history. Circle's visible schema lists both Chat events and Posts events [3_choose_the_official_ingestion_path[4]] [14], but validate the exact event payload before committing to message-text extraction.

## 4. Implementation Skeleton

The following Python is deliberately an API client, not a browser scraper. It assumes the community operator has supplied an authorized token and allowlisted content IDs.

```python
import os
import requests

BASE = "https://app.circle.so"
TIMEOUT = 30


def request_json(method, path, bearer, **kwargs):
    headers = {
        "Authorization": f"Bearer {bearer}",
        "Content-Type": "application/json",
    }
    response = requests.request(
        method,
        BASE + path,
        headers=headers,
        timeout=TIMEOUT,
        **kwargs,
    )
    response.raise_for_status()
    return response.json()


def mint_member_jwt(headless_auth_token, community_member_id):
    return request_json(
        "POST",
        "/api/v1/headless/auth_token",
        headless_auth_token,
        json={"community_member_id": community_member_id},
    )


def collect_allowed_member_content(member_jwt, allowed_space_ids,
                                   allowed_chat_room_uuids):
    output = []

    for space_id in allowed_space_ids:
        posts = request_json(
            "GET",
            f"/api/headless/v1/spaces/{space_id}/posts",
            member_jwt,
        )
        output.append(("posts", space_id, posts))

    for room_uuid in allowed_chat_room_uuids:
        messages = request_json(
            "GET",
            f"/api/headless/v1/messages/{room_uuid}/chat_room_messages",
            member_jwt,
        )
        output.append(("chat_room_messages", room_uuid, messages))

    return output
```

Do not hardcode undocumented response fields or pagination behavior. Generate the production client from Circle's OpenAPI specification or map the actual documented schema for each endpoint. The Member API reference confirms the routes but does not expose one universal pagination field set for all of them [4_implementation_skeleton[0]] [15].

Keep every community isolated in configuration:

```yaml
community_id: "..."
permission_status: approved
approved_purpose: software-development request detection
allowed_space_ids: ["..."]
allowed_chat_room_uuids: ["..."]
excluded_content: [direct_messages, member_bios, email_addresses]
retention_days: 30
outreach_mode: reply_in_original_thread
operator_contact: "..."
approval_reference: "..."
```

Never log bearer tokens or raw session cookies. Store each community token in a secrets manager, rotate it after staff changes, and stop collection immediately when authorization is revoked. Circle documents monthly Admin API allowances and an IP limit of 2,000 requests per five minutes, with 429 backoff recommended [4_implementation_skeleton[1]] [9]. Design below those ceilings rather than treating them as throughput targets.

## 5. Parse Buying Intent Without Building A Member Dossier

Normalize each approved post or chat message into a minimal record:

```text
community_id
space_or_room_id
source_content_id
thread_id
permalink
published_at
author_display_name
text_for_classification
permission_reference
collected_at
```

Do not enrich it automatically with personal emails, phone numbers, unrelated profile history, or cross-community identity matching. Hash the member ID if the original identifier is not needed for replying in context.

Apply a transparent score before using an LLM:

| Signal | Score |
|---|---:|
| Explicit request: "looking for", "need", "recommend", or "want to hire" | +35 |
| Software deliverable named: app, website, integration, API, automation | +20 |
| Budget, deadline, launch date, or procurement process stated | +15 |
| Asks for referrals, proposals, estimates, or availability | +15 |
| Poster appears able to commission work | +10 |
| Vendor self-promotion | -35 |
| Developer looking for employment rather than buying services | -30 |
| Negation such as "not hiring" | -40 |
| Educational or hypothetical discussion | -20 |

Send records scoring 55 or more to an LLM that returns strict JSON:

```json
{
  "is_buying_intent": true,
  "requested_service": "Shopify to ERP integration",
  "evidence_quote": "We need someone to connect...",
  "budget_signal": "not stated",
  "timeline_signal": "before November launch",
  "confidence": 0.86,
  "disqualifiers": [],
  "recommended_action": "human_review"
}
```

Require the model's `evidence_quote` to be an exact substring of the source. Reject outputs that invent budgets, deadlines, company names, or contact information. Keep the original surrounding thread available to the reviewer because a single sentence can reverse meaning when separated from its parent message.

A hypothetical example illustrates the distinction. "We need a React developer to finish our customer portal before launch; recommendations?" is high-intent because it contains a buyer, deliverable, timing, and referral request. "I am a React developer looking for projects" is supply, not buying intent. "We are not hiring developers this quarter" must be removed by the negation rule even though it contains the word `hiring`.

## 6. Human Review, Outreach, And Retention

The review queue should display the community, approved space, source permalink, surrounding thread, evidence quote, score, and permission basis. It should not display scraped profile dossiers. A reviewer must answer four questions:

1. Is this an actual request for outside help?
2. Does the community permit commercial responses?
3. Does the operator's approval cover this space and intended use?
4. Is replying in the original thread more appropriate than a DM?

Prefer a useful public reply where community norms permit it. Do not automatically DM every candidate. The first contact should disclose who you are, reference the member's request, and avoid implying an endorsement from Circle or the community operator.

Set short retention by default: for example, delete rejected candidates immediately, expire uncontacted candidates after 30 days, and retain only outcome statistics after deleting message text. Provide an operator kill switch that revokes a community, deletes its stored content, and prevents re-ingestion. Maintain an audit log of collection time, endpoint, approved space, classifier version, review decision, and deletion time.

If an operator wants AI-assisted access without building a warehouse pipeline, Circle documents a custom MCP app workflow. It can be created under `Settings -> Apps -> Create`; workspace-admin installations begin as Draft and must be published [6_human_review_outreach_and_retention[0]] [19]. Circle also documents a read-only mode in which the AI can view community resources but cannot create, change, or delete them [6_human_review_outreach_and_retention[0]] [19]. Treat that as a bounded review option, not evidence that an ordinary member may install a cross-community harvesting agent.

## 7. Failure Modes And Stop Conditions

Stop rather than work around any of these conditions:

- The community requires approval and has not granted it.
- The operator declines the proposed processing purpose.
- A private space returns a lock screen or an API returns 401 or 403.
- Access would require copied browser cookies, reverse-engineered endpoints, CAPTCHA bypass, or repeated account creation.
- The only available content is a DM or small private group not covered by explicit consent.
- Community rules prohibit solicitation, lead generation, automated analysis, or off-platform outreach.
- The operator or a member requests deletion.
- Circle asks you to suspend the integration.

Do not interpret a direct URL as authorization. Circle explicitly notes that content access depends on configuration and membership even when a space is reachable by URL [7_failure_modes_and_stop_conditions[0]] [5]. Do not rotate accounts or IP addresses to evade limits; Circle prohibits circumventing access controls and overburdening the service [7_failure_modes_and_stop_conditions[1]] [6].

## Synthesis

The original concept combines three distinct acts: discovering communities, joining them, and processing conversations. They have different permission boundaries.

Discovery can use public Circle Discover listings. Enrollment can use your real account through each community's ordinary signup. Neither step authorizes automated extraction. The decisive boundary is ingestion: posts should come from an operator-approved Admin API integration, chats from an operator-enabled Headless Member API flow, and high-volume event history from a community-controlled Data API feed.

The trade-off is straightforward. Browser scraping appears easiest because it reuses your login, but it has the weakest authorization, greatest breakage risk, and clearest conflict with Circle's terms. Admin API v2 is reliable for posts but does not provide the documented chat read path. Headless provides member-visible chat-room messages, but only after the community creates the required authentication token. Data API is best for incremental analytics but requires operator-level product access and payload validation. Circle MCP can support bounded read-only review but should not be assumed to authorize cross-workspace bulk collection.

The recommended system is therefore not "one account that scrapes every free community." It is a network of explicitly approved, community-scoped integrations feeding a minimal lead-classification pipeline. That model takes longer to establish, but it preserves access, produces cleaner data, gives operators control, and supports respectful engagement instead of covert surveillance or spam.

## References

1. *Quick start*. https://api.circle.so/apis/admin-api/quick-start.md
2. *Different ways to configure community access and visibility | Circle Knowledge Base*. https://help.circle.so/p/basics/community-access/different-ways-to-configure-community-access-and-visibility
3. *Admin API*. https://api.circle.so/apis/admin-api
4. *Headless*. https://api.circle.so/apis/headless.md
5. *Managing space access and visibility | Circle Knowledge Base*. https://help.circle.so/p/basics/spaces/managing-space-access-and-visibility
6. *Platform Terms of Service | Circle*. https://circle.so/terms
7. *Circle Developers*. http://api.circle.so/llms.txt
8. *Privacy Notice | Circle*. https://circle.so/privacy
9. *Usage and limits*. https://api.circle.so/apis/admin-api/usage-and-limits
10. *Circle Discover*. https://discover.circle.so/
11. *Quick start*. https://api.circle.so/apis/headless/quick-start.md
12. *Messages*. https://api.circle.so/get-started/concepts/messages
13. *Data API*. https://api.circle.so/apis/data-api
14. *Data API*. https://app.circle.so/data_api/docs
15. *Swagger UI*. https://api-headless.circle.so/?urls.primaryName=Member%20APIs
16. *Swagger UI*. https://api-headless.circle.so/?urls.primaryName=Admin%20API%20V2
17. *http://circle.so/robots.txt*. http://circle.so/robots.txt
18. *Circle Discover — Find your next 1,000 members*. https://circle.so/discover
19. *Overview*. https://api.circle.so/llms-full.txt
