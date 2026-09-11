-- P19: classify how each community can be joined (free / paid / invite-only).
-- ============================================================================
-- Adds communities.join_type + join_type_checked_at. Populated by
-- discovery/join_type.py's fetch_join_classification(), called from
-- harvest.py on every read of every known community (not just new finds), so
-- this backfills itself on the next harvest run -- no data migration needed,
-- only the columns.
--
-- Both columns are also added automatically by Database._ensure_columns() on
-- the next worker/dashboard start, so running this by hand is not urgent,
-- only immediate (useful to query before the next deploy).
--
-- join_type values: free_join | paid | invite_only | locked_unknown | unknown
-- See circle_leads/discovery/join_type.py for what each means.
-- ============================================================================

BEGIN;

ALTER TABLE communities ADD COLUMN IF NOT EXISTS join_type VARCHAR(32);
ALTER TABLE communities ADD COLUMN IF NOT EXISTS join_type_checked_at TIMESTAMP;

CREATE INDEX IF NOT EXISTS ix_communities_join_type ON communities (join_type);

COMMIT;

-- Verify after the next harvest run:
--   SELECT join_type, count(*) FROM communities GROUP BY join_type ORDER BY 2 DESC;
