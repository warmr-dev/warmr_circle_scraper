-- P8b — register the team's remaining "Joined" communities
-- ============================================================================
-- From the team comment, 6 communities are Joined. 3 already have rows + cookies
-- (silicon-slopes, spin-state, big-star-lights -- see P10). The other 3:
--
--   trigify-social-circle -> trigify-social-circle.circle.so
--       9 PUBLIC spaces -> harvest reads it with no cookie once the row exists.
--   forum                 -> forum.joelpilger.com   (already row id 689)
--       23 PUBLIC spaces -> readable once platform='circle' (P8a sets it) and
--       the custom-domain harvest support ships (this PR).
--   skl-club              -> members.skl.club   (row id 691 has the Discover URL)
--       0 public spaces -> needs a session cookie connected in the dashboard.
--
-- Run P8a first (or after -- both set platform).
-- ============================================================================

BEGIN;

-- trigify: new row, a plain circle.so subdomain, publicly readable.
INSERT INTO communities (slug, name, url, platform, discovered_at, discovery_source,
                         access_status, permission_status, relevance_score,
                         relevant, watching, updated_at)
SELECT 'trigify-social-circle', 'Trigify Social Circle',
       'https://trigify-social-circle.circle.so', 'circle', now(), 'manual:team-joined',
       'visited', 'candidate', 30, true, true, now()
WHERE NOT EXISTS (SELECT 1 FROM communities
                  WHERE url = 'https://trigify-social-circle.circle.so'
                     OR slug = 'trigify-social-circle');

-- skl-club: repoint the existing Discover-listing row to the real custom domain.
UPDATE communities
   SET url = 'https://members.skl.club',
       platform = 'circle',
       discovery_source = 'manual:team-joined',
       access_status = 'visited',
       relevant = true,
       watching = false,               -- 0 public spaces; needs a cookie, not the harvest
       updated_at = now()
 WHERE slug = 'skl-club'
   AND url ILIKE 'https://discover.circle.so/products/%';

-- forum (id 689) already exists; P8a sets platform='circle'. Put it on the
-- watchlist so the harvest picks it up promptly once custom-domain support ships.
UPDATE communities
   SET watching = true, relevant = true, updated_at = now()
 WHERE url = 'https://forum.joelpilger.com';

-- Verify:
--   SELECT slug, url, platform, watching FROM communities
--    WHERE slug IN ('trigify-social-circle','skl-club') OR url = 'https://forum.joelpilger.com';

COMMIT;
