-- P20: bulk Circle directory discovery + ICP relevance filter + auto-join
-- lifecycle. Part of the pivot recorded in WORKLOG.md (2026-09-14): discovery
-- moves from per-niche web search to a one-time bulk crawl of Circle's own
-- discovery API, gated by an ICP (software-dev-company) relevance filter, then
-- auto-joined by a new Playwright bot (circle_leads/join/) for the free/open ones.
-- ============================================================================
-- All columns are also added automatically by Database._ensure_columns() on the
-- next worker/dashboard start, so running this by hand is not urgent, only
-- immediate (useful to query before the next deploy).
--
-- communities.external_directory_id / directory_goals / directory_synced_at:
--   provenance from discovery/circle_directory.py's crawl of
--   discover.circle.so/backend/public_api.
-- communities.icp_score / icp_flag / icp_reasons / icp_checked_at / icp_decided_by:
--   classifier/icp_relevance.py's verdict on fit for a software-dev-company's
--   lead gen. Deliberately separate from the older relevance_score/relevant
--   columns (the web-search ranking heuristic) until the new classifier has a
--   track record -- see WORKLOG.md.
-- communities.join_status / join_status_detail / join_attempted_at / joined_at /
-- join_attempts: outcome of the auto-join bot's attempt (circle_leads/join/).
--   Independent of the existing join_type column: join_type is a property of the
--   community (how it CAN be joined, from discovery/join_type.py), join_status is
--   what OUR bot actually did about it.
-- replay_sessions.source: "extension" (Chrome cookie-capture extension) vs
--   "join_bot" (minted directly by the new auto-join bot) -- observability only.
-- ============================================================================

BEGIN;

ALTER TABLE communities ADD COLUMN IF NOT EXISTS external_directory_id VARCHAR(64);
ALTER TABLE communities ADD COLUMN IF NOT EXISTS directory_goals JSON;
ALTER TABLE communities ADD COLUMN IF NOT EXISTS directory_synced_at TIMESTAMP;

ALTER TABLE communities ADD COLUMN IF NOT EXISTS icp_score FLOAT DEFAULT 0.0;
ALTER TABLE communities ADD COLUMN IF NOT EXISTS icp_flag BOOLEAN DEFAULT FALSE;
ALTER TABLE communities ADD COLUMN IF NOT EXISTS icp_reasons JSON;
ALTER TABLE communities ADD COLUMN IF NOT EXISTS icp_checked_at TIMESTAMP;
ALTER TABLE communities ADD COLUMN IF NOT EXISTS icp_decided_by VARCHAR(32);

ALTER TABLE communities ADD COLUMN IF NOT EXISTS join_status VARCHAR(32) DEFAULT 'not_attempted';
ALTER TABLE communities ADD COLUMN IF NOT EXISTS join_status_detail TEXT;
ALTER TABLE communities ADD COLUMN IF NOT EXISTS join_attempted_at TIMESTAMP;
ALTER TABLE communities ADD COLUMN IF NOT EXISTS joined_at TIMESTAMP;
ALTER TABLE communities ADD COLUMN IF NOT EXISTS join_attempts INTEGER DEFAULT 0;

ALTER TABLE replay_sessions ADD COLUMN IF NOT EXISTS source VARCHAR(16) DEFAULT 'extension';

CREATE INDEX IF NOT EXISTS ix_communities_external_directory_id ON communities (external_directory_id);
CREATE INDEX IF NOT EXISTS ix_communities_icp_score ON communities (icp_score);
CREATE INDEX IF NOT EXISTS ix_communities_icp_flag ON communities (icp_flag);
CREATE INDEX IF NOT EXISTS ix_communities_join_status ON communities (join_status);

COMMIT;

-- Verify after the first `discover-directory` + `filter-relevant` run:
--   SELECT count(*) FROM communities WHERE external_directory_id IS NOT NULL;
--   SELECT icp_flag, count(*) FROM communities GROUP BY icp_flag;
--   SELECT join_status, count(*) FROM communities GROUP BY join_status ORDER BY 2 DESC;
