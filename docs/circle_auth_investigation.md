# Does Circle offer member authentication for an external app?

**No.** Verified 2026-09-07 against Circle's **live OpenAPI specs**, not just the
prose docs. There is no member OAuth, no member personal-access token, and no
endpoint that lists the communities a person belongs to.

This document exists so the question does not have to be re-litigated. If you
revisit it, re-check the specs — not a blog post, and not the marketing pages.

---

## Where the authoritative specs are

The docs site links a Swagger UI whose spec URLs are only discoverable from its
initializer (`https://api-headless.circle.so/swagger-initializer.js`). The
`/openapi/*.json` paths people usually guess all 404.

```
https://api-headless.circle.so/api/headless_auth/swagger.yaml       Auth API
https://api-headless.circle.so/api/headless_client/v1/swagger.yaml  Member API
https://api-headless.circle.so/api/admin/v2/swagger.yaml            Admin API v2
```

Plus a documentation index for LLMs at `https://api.circle.so/llms.txt`, and any
docs page as Markdown by appending `.md`.

## The evidence

### 1. There is no OAuth anywhere in the platform

```
$ grep -ic oauth headless_auth_swagger.yaml headless_client_v1_swagger.yaml admin_v2_swagger.yaml
headless_auth_swagger.yaml:0
headless_client_v1_swagger.yaml:0
admin_v2_swagger.yaml:0
```

No `authorizationCode`, no `implicit`, no `authorizationUrl`. Every
`securityScheme` in all three specs is a bearer token or an API key.

The complete developer-platform index (`llms.txt`) lists exactly four API
products — Admin API, Headless (Member + Auth), Data API, Circle Plus — plus an
MCP server. There is no OAuth product to have missed.

### 2. The member JWT is minted by an admin, without the member

`POST /api/v1/headless/auth_token`, authenticated with the **admin's** Headless
Auth token, takes exactly one of:

```yaml
oneOf:
  - required: [sso_user_id]
  - required: [community_member_id]
  - required: [email]
```

No password. No redirect. No consent screen. The member never participates —
an admin names a member and receives a token for them. That is impersonation
by the community's own operator, which is why it only works for a community you
administer. The 403 says so plainly:

> "Your community isn't eligible for headless API access. Please upgrade or
> contact support"

The *community* is what is authorized, not the member.

### 3. The token is locked to one community

The JWT payload carries a single hard-coded `community_id` (1h access token,
1mo refresh). Decoding the spec's own example:

```json
{"community_id": 1, "community_member_id": 1, "sso_user_id": "…", "exp": …}
```

### 4. Nothing lists "my communities"

The Member API has 78 paths, all singular-community: `/community_member`,
`/spaces`, `/spaces/home`, `/home`, `/community_members/{id}/…`. There is **no
`/communities` collection endpoint**. Circle has no concept of "the communities
this person belongs to" in its API surface.

### 5. Tokens are admin-only to create

> "Community admins can obtain an API key by going to the **Developers → Tokens**
> page in their community and selecting the type as **Headless Auth**."

Business plan and above.

### 6. Circle's OAuth support points the other way

Circle's "custom SSO via OAuth" makes Circle the OAuth **client**, consuming
*your* identity provider so members log in to *your* community with a "Continue
with …" button. Circle is never the authorization server, so there is nothing
for a third-party app to be authorized *by*.

### 7. Even Zapier uses an admin token

Circle's flagship integration authenticates by pasting an **admin-created API
token**. If a member-authorization path existed, the highest-volume integration
partner on the platform would be using it.

## Conclusion

| Mechanism | Exists? | Who authorizes | Scope |
| --- | --- | --- | --- |
| Member OAuth (member authorizes an app) | **No** | — | — |
| Member personal access token | **No** | — | — |
| Headless Auth → member JWT | Yes | **Community admin** | One community |
| Admin API v1/v2 | Yes | Community admin | One community |
| Data API | Yes | Community admin | One community |
| "List my communities" | **No** | — | — |

An admin API token is **not** equivalent to a normal member's personal
authorization, and no amount of configuration turns one into the other.

**Therefore:** reading the private communities *your own account* is enrolled in
can only be done through a real browser session that you personally establish.
That is the local connector (`docs/connector.md`).

Putting that browser on the server was tried and measured — Circle serves it a
Cloudflare challenge. See `docs/remote_browser_poc.md`.
