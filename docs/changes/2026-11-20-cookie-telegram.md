# 2026-11-20 — Telegram when a community login dies

The worker and the feed watcher already knew when a stored session stopped
working. That showed up on the dashboard and nowhere else, so a person found
out only by opening Needs attention.

A login that has just been refused now sends one Telegram message: project
Warmr, the community's name, and that the cookies have to be sent again.
A community set to paused stays quiet. The same refusal is not sent again
until a later read succeeds. The bot is the droplet's `TELEGRAM_BOT_TOKEN`
and `TELEGRAM_CHAT_ID`. The dashboard's "service is silent" check is
unchanged and still uses the same variable names in its own environment.
