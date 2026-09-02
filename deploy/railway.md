# Deploying to Railway / Out Plane

Two services share one Postgres database. Set `CIRCLE_LEADS_DB` to the
Postgres connection URL on both.

## Service 1 — dashboard (web)
```
CMD: circle-leads --db "$CIRCLE_LEADS_DB" dashboard --host 0.0.0.0 --port $PORT
Env: CIRCLE_LEADS_DB, DASHBOARD_PASSWORD, DASHBOARD_SECRET_KEY, DASHBOARD_HTTPS=true
```

## Service 2 — search worker (background)
A long-running worker that searches on an interval:
```
CMD: circle-leads --db "$CIRCLE_LEADS_DB" search-watch "flutter developer" "startup founders" --interval 86400 --min-score 25
Env: CIRCLE_LEADS_DB, BRAVE_API_KEY (recommended for reliable search)
```

Or, if the platform has a cron/scheduled-job feature, use a one-shot instead
(drop `--interval`), scheduled daily:
```
CMD: circle-leads --db "$CIRCLE_LEADS_DB" search-watch "flutter developer" --min-score 25
```

Notes:
- Postgres is required in the cloud; SQLite does not persist on ephemeral disks.
- Set BRAVE_API_KEY — the keyless fallback rate-limits under a server's IP.
- New finds appear in the dashboard's Communities tab; nothing is auto-joined.
