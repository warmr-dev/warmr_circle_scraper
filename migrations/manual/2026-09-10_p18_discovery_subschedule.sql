-- P18: give the harvest's discovery (web-search) phase its own schedule.
--
-- Background: every harvest has two phases.
--   1. DISCOVER new communities via web search (Exa + DuckDuckGo). Exa is
--      metered (ran out of credits mid-run on 2026-09-10) and DuckDuckGo
--      rate-limits the worker IP after a few requests.
--   2. READ the communities we already know -- free conditional GETs, this is
--      where the leads actually come from.
--
-- Until now every harvest ran phase 1. At every_6h that is 4 full web-search
-- sweeps a day (~12-25 niches each), which is what exhausted the Exa credits.
--
-- After the matching code change the worker reads `settings.harvest_search`:
--   every_run  -> search on every harvest (old behaviour)
--   every_6h / every_12h / daily / weekly / custom:<n>  -> search only when
--                that interval has elapsed since settings.harvest_last_search
--   off        -> never search (read-only harvests)
-- Reading known communities is unaffected and still runs every harvest.
--
-- This sets it to once a day. Reading stays on harvest_schedule (every_6h).

INSERT INTO settings (key, value) VALUES ('harvest_search', 'daily')
ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value;

-- Optional: also seed the last-search timestamp so the next harvest does NOT
-- immediately search (it would otherwise, since the key is unset). Comment out
-- if you want one discovery pass on the next run.
-- INSERT INTO settings (key, value) VALUES ('harvest_last_search', now()::text)
-- ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value;

SELECT key, value FROM settings WHERE key IN ('harvest_schedule', 'harvest_search', 'harvest_last_run', 'harvest_last_search');
