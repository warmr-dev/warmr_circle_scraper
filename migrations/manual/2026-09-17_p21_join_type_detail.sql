-- P21: persist WHY a community got a given join_type, not just the bucket.
-- "unknown" alone doesn't say whether a host is dead, timed out, or returned
-- something odd -- fetch_join_classification() already computes this detail,
-- it just wasn't being saved. Also applied automatically by
-- circle_leads/storage/database.py::_ensure_columns() on next boot -- running
-- this by hand isn't urgent, just immediate.

BEGIN;

ALTER TABLE communities ADD COLUMN IF NOT EXISTS join_type_detail TEXT;

COMMIT;

-- Verify:
-- SELECT column_name FROM information_schema.columns
--   WHERE table_name = 'communities' AND column_name = 'join_type_detail';
