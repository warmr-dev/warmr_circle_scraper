# Warmr Circle Cookie Sync (browser extension)

Replaces the copy-paste-into-DevTools step in the dashboard's **Join Queue**
tab with one click. You still do the human part yourself — log in, join,
clear any onboarding — this only automates handing the resulting session
cookie to the dashboard.

**What it does:** reads `_circle_session` + `user_session_identifier` (+
`remember_user_token` if present) for the tab you're currently on, via the
browser's privileged cookie API (these are almost certainly `HttpOnly`, so a
bookmarklet or page script cannot read them — an extension is the minimum
that works), and POSTs them to
`POST /api/connections/{host}/session` — the exact endpoint the dashboard's
own cookie-paste form calls.

**What it does not do:** log in, click Join, solve a CAPTCHA, or fill any
form. Circle's login/signup pages run Cloudflare Turnstile, which is a wall
against automation, not something this — or anything else here — tries to
get past. See `docs/remote_browser_poc.md` for why that path is closed.

## Install (unpacked, personal use)

This is not published to the Chrome Web Store — it's a small internal tool,
loaded the same way any local extension is:

1. **Server side, once:** set `EXTENSION_API_TOKEN` in the dashboard's
   environment (Vercel → Project → Settings → Environment Variables) to a
   long random string. Without it, this extension cannot authenticate — the
   dashboard's normal login still works regardless.
2. Chrome/Edge/Brave → `chrome://extensions` → enable **Developer mode** →
   **Load unpacked** → select this `extension/` folder.
3. Click the extension's icon → **Settings** → set:
   - **Dashboard URL** — defaults to `https://warmr-circle-scraper.vercel.app`.
   - **Extension token** — the same value as `EXTENSION_API_TOKEN` above.

## Use

1. Open the **Join Queue** tab in the dashboard, pick a community, open its
   join page (the extension doesn't do this step).
2. Log in with the right account (test account for `free_join`, main account
   for `paid`) and join.
3. On that community's tab, click the extension icon → **Send session
   cookie**.
4. The row disappears from Join Queue on its own on the next refresh — the
   worker reads it from here on, no further action needed until the cookie
   expires and the community reappears under "Cookies to refresh".

## Permissions, and why

`manifest.json` requests `cookies` + `storage` and `host_permissions:
["<all_urls>"]`. The broad host permission is needed because communities live
on arbitrary custom domains (`www.siliconslopes.com`,
`community.bigstarlights.com`), not just `*.circle.so` — a narrower pattern
would miss those. In spite of the broad *grant*, the extension's *behavior*
stays narrow: it only ever reads cookies for the tab you have open when you
click the button, never scans other sites, and the token it sends is scoped
server-side to one route (`POST /api/connections/{host}/session`) — it
cannot read leads, change config, or do anything else on the dashboard (see
`require_auth_or_extension_token` in `circle_leads/web/app.py`).

## Threat model, briefly

- The extension token lives in `chrome.storage.local` on your machine, sent
  only to the dashboard URL you configured, over HTTPS.
- If it leaks, the worst a holder can do is store an arbitrary cookie blob
  under a host name of their choosing — annoying (bad data for one
  community), not a data-exfiltration path (the same endpoint the dashboard's
  own paste-a-cookie form already exposes to anyone with your dashboard
  password).
- Rotate `EXTENSION_API_TOKEN` if it's ever exposed; every install using the
  old value stops working immediately.
