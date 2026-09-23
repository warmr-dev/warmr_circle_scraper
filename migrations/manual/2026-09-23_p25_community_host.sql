-- p25: one host = one community.
--
-- `get_or_create_community` used to match on an exact `url` string, so a
-- community entered from a directory link (`/join?invitation_token=...`,
-- `?utm_source=circle_discover`) and again from its bare address became two
-- rows on one host. Both rows then collected the same posts and both produced
-- leads: on 2026-09-23 that was 121 doubled posts on
-- community.launchthedamnthing.com, 38 on community.practicommunity.com.
--
-- Order matters: run scripts/merge_duplicate_communities.py --apply BEFORE the
-- unique index at the bottom, or it will fail on the rows that still double.

ALTER TABLE communities ADD COLUMN IF NOT EXISTS host varchar(255);

UPDATE communities
   SET host = nullif(lower(split_part(split_part(url, '//', 2), '/', 1)), '')
 WHERE host IS NULL;

-- Circle's directory lists many unrelated communities under one host, so a
-- `discover.circle.so` address identifies nothing.
UPDATE communities
   SET host = NULL
 WHERE host IN ('discover.circle.so', 'circle.so', 'www.circle.so');

CREATE INDEX IF NOT EXISTS ix_communities_host ON communities (host);

-- The guard. Partial, because NULL hosts (directory rows) may repeat.
CREATE UNIQUE INDEX IF NOT EXISTS uq_communities_host
    ON communities (host) WHERE host IS NOT NULL;
