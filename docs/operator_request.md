# Requesting operator approval

Ingestion requires the community operator's explicit approval. Join the
community normally under your real identity first, take part genuinely, then
ask. Do not automate signup, and do not send repeated join requests.

## Template

> Hi [name],
>
> I'm a member of [community]. I run a small tool that identifies posts where
> members are explicitly asking for software-development help, so I can offer to
> help where it's actually wanted.
>
> Would you be willing to let me process posts from these spaces?
>
> - [space]
> - [space]
>
> Specifically:
>
> - **Read-only.** Nothing is posted, changed, or deleted.
> - **Only the spaces you name.** No DMs, no member bios, no profile scraping,
>   no contact-detail collection.
> - **Retention: 30 days**, then automatic deletion.
> - **A human reviews every candidate** before any outreach. Nothing is
>   automated on the contact side.
> - **Your outreach rules apply.** I'll reply in-thread rather than DM if you
>   prefer, or not at all in spaces you'd rather keep clear.
> - **Access through a token you control** — a read-only Admin API token, or a
>   member-scoped Headless integration. You can revoke it at any time and I'll
>   delete the stored content on request.
>
> Happy to show you exactly what it collects first.
>
> Thanks,
> [name]

## After a reply

Record the outcome in `circle_leads/config/communities/<community>.yaml`:

```yaml
permission_status: approved      # candidate | contacted | approved | denied | revoked
approved_purpose: "Detect posts explicitly requesting software-development help"
allowed_space_ids: ["123", "456"]     # only what they named
allowed_chat_room_uuids: []
operator_contact: "operator@example.com"
approval_reference: "Email thread 2026-09-01"
retention_days: 30
outreach_mode: reply_in_original_thread
admin_token_env: "CIRCLE_ADMIN_TOKEN__acme"   # variable NAME, not the token
```

Only `permission_status: approved` enables ingestion, and only the spaces listed
are read. An empty `allowed_space_ids` collects nothing.

## Stop conditions

Stop rather than work around any of these:

- Approval was never granted, or has been withdrawn.
- The operator declines the processing purpose.
- An API returns 401 or 403, or a space shows a lock screen.
- Access would need copied cookies, undocumented endpoints, CAPTCHA bypass, or
  extra accounts.
- The only available content is a DM or a small private group.
- Community rules prohibit solicitation, lead generation, or automated analysis.
- An operator or member asks for deletion — run
  `circle-leads purge --community <slug>`.
- Circle asks you to suspend the integration.
