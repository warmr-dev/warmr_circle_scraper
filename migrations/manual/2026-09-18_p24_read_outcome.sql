-- P24: remember what the last harvest read actually saw, so the re-check
-- interval follows it: "public" / "private" (401/403) / "gone" (404 or the
-- host no longer maps to a community) / "error" (network failure, 429, 5xx).
-- Before this, a failed read looked like "no posts" and a transient error
-- could park a community for a week.
--
-- Also applied automatically by
-- circle_leads/storage/database.py::_ensure_columns() on the next boot of any
-- process WITHOUT SKIP_DB_INIT=true. Vercel runs with SKIP_DB_INIT=true, so
-- apply this BEFORE deploying code that has the column in the model --
-- otherwise every ORM read of communities there fails with "column does not
-- exist". Nullable, no default: a metadata-only change in Postgres, no rewrite.

BEGIN;

ALTER TABLE communities ADD COLUMN IF NOT EXISTS read_outcome VARCHAR(32);

COMMIT;

-- Verify:
-- SELECT column_name FROM information_schema.columns
--   WHERE table_name = 'communities' AND column_name = 'read_outcome';
