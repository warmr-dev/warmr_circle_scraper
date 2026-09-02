# Reading a feed you're a member of (Python, automatic)

For communities where you are a genuine, logged-in member, `read-feed` pulls a
space's posts through Circle's internal feed endpoints — the same ones your
browser calls — and runs them through the lead classifier. No manual copy/paste.

## What this is, honestly

- It calls Circle's **internal** (`/internal_api/…`) endpoints, which are not a
  documented public API. Circle's terms discourage automated access to them, so
  this is built as *a member reading their own feeds at a polite rate*: it's
  read-only, rate-limited, stops on the first 401/403, and touches only the
  spaces you name. It never posts, joins, or changes anything.
- If Circle changes these endpoints or blocks your session, the command fails
  cleanly — that's expected; the internal API carries no stability guarantee.
- Only use it for communities you actually belong to.

## Step 1 — Copy your session cookie (once)

The endpoints authenticate with your browser session, so `read-feed` needs your
Cookie header. You copy it once:

1. Open the community in your browser and sign in.
2. Open DevTools → **Network** tab.
3. Reload. Click any `internal_api/...` request.
4. Under **Request Headers**, find **Cookie** and copy its entire value.

Put it in your environment (it's read at call time, never stored or logged):

```bash
export CIRCLE_SESSION_COOKIE='<paste the whole Cookie value here>'
```

The cookie expires (days to weeks). When `read-feed` says the session was
rejected, repeat this step.

## Step 2 — Get the space IDs you want to read

The feed endpoint uses a numeric space id, not the `/c/<slug>` you see in the
URL. To find it:

1. Open the space (e.g. the Job Posts space) in your browser.
2. DevTools → Network → reload → look for a request like
   `internal_api/spaces/1595123/posts`. The number is the space id.

For **Startup&Angels**, the lead-rich spaces are:

| Space | Slug | ID |
|---|---|---|
| 💼 Job Posts | `job-posts` | `1595123` |
| 🤝 Opportunities & Collabs | `opportunities-collabs` | *(look it up as above)* |
| 🚀 Community Posts | `community` | *(look it up)* |
| 🤝 Ask an Expert | `service-providers` | *(look it up)* |

## Step 3 — Read the feed

```bash
circle-leads read-feed startupandangels.circle.so \
  --space-id 1595123 \
  --community startupandangels
```

Add more `--space-id` flags to read several spaces in one run. Output is the
ranked leads; they also land in the dashboard's Leads tab.

```bash
# several spaces, escalate ambiguous posts to the AI
circle-leads read-feed startupandangels.circle.so \
  --space-id 1595123 --space-id <other-id> --use-llm
```

## Notes

- It reads the *truncated* post body (~255 chars) plus the title, which is
  enough to classify hiring intent. The full body is only in the post's own
  page; open the permalink if you need it before replying.
- Re-running is safe: posts are de-duplicated, so a second run only classifies
  what's new.
- Everything else works as before: `search`, `export`, the dashboard.
