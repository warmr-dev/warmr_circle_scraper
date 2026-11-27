# 2026-11-27 — Rest a host after a browser death, and send portal slugs the portal accepts

Two production failures, one release. Neither changes the schema. This change
does not write to the production database, and it does not deploy.

## Join queue: bridge_error cooldown

When the droplet browser died during a join (`bridge_error`, for example
"Target page, context or browser has been closed" or "the browser went away"),
the batch stopped and wrote an activity-log error, but it did not write the
attempt log or the community row. The 15-minute join timer therefore picked
the same host again. haven-online-safety was visited 282 times from
2026-11-24 through 2026-11-27. intouxdesign was picked again at 14:46 and
15:02 on 2026-11-27.

A death after the page was opened now records the attempt. The queue skips
that host for 6 hours. Three deaths on the same host inside 3 days set
`join_status` to `needs_human`. A person opens the community by hand, then
sets `join_status` back to `not_attempted`. A death before any page opens
still stops the batch and does not rest the host. The Attention page shows
join errors from the last 24 hours. The overview still treats any
`join_attempted_at` inside 7 days as not waiting; the automatic queue uses
the 6-hour attempt-log rest and will try the host again after that.

## Community intake: slug

Intake sent `communities.slug`. On a custom domain that value is the raw host,
and the portal rejects it (`slug must use lowercase letters, digits, and
hyphens`). One such item made the chunk register 0. The payload now sends
`community_slug_for_host` of the host. The stored slug is unchanged.

| host | slug sent |
|---|---|
| community.everythingmarketplaces.com | everythingmarketplaces |
| community.exitfive.com | exitfive |
| community.steepinsights.com | steepinsights |
| www.firefly-innovations.org | firefly-innovations |
| www.startup-ready.com | startup-ready |
| www.hailerz.com | hailerz |

A label that is still illegal after that is left out of the POST. A portal
4xx or an unverifiable receipt on a multi-item body is retried one community
at a time. A 5xx or a connection error is not multiplied.

`www.hailerz.com` is already cached under the dotted slug. The cache is a
fingerprint of the payload, so the normalized slug does not match. The next
ordinary intake after deploy resends it as `hailerz`. Do not clear
`settings.community_intake_state` and do not pass `--force`: that would resend
every monitored community. The portal may already have a rejected candidate
under the dotted slug; this resend does not rename or delete that row. That
cleanup needs its own approval. This pull request performs no production
database write.

## Deploy

Manual, after review and merge:

```sh
deploy/droplet/deploy_release.sh <sha>
```

The dashboard on Vercel needs the same commit so Attention shows join errors.
No migration. No environment variable.

## Delivery

- Local: implemented and unit-tested in the task worktree.
- Committed: this branch.
- PR: opened for review. Not merged.
- Merged: no.
- Deployed/Applied: no. Droplet and Vercel are unchanged. No production database write.
- Live verified: no.
- Workspace closed: no.
