-- P8a — backfill communities.platform
-- ============================================================================
-- The `platform` column is added automatically by Database._ensure_columns()
-- on the next worker/dashboard start. This backfills the 138 existing rows so
-- the harvest's platform gate (`platform = 'circle'`) has values to read; until
-- then those rows fall back to the old URL-shape check, so running this is not
-- urgent, only tidy.
--
-- Buckets (verified against the 2026-09-10 snapshot):
--   circle        98 single-label *.circle.so + 4 custom Circle domains
--   discover      32 discover.circle.so/products/<slug> listings (host unresolved)
--   circle_infra   3 login.circle.so, discover.circle.so, email.notification.circle.so
--   other          1 manual://flutter-devs
-- ============================================================================

BEGIN;

-- discover.circle.so/products/* -> a listing, real host not resolved yet
UPDATE communities SET platform = 'discover'
 WHERE platform IS NULL AND url ILIKE 'https://discover.circle.so/products/%';

-- Circle infrastructure hosts, not communities
UPDATE communities SET platform = 'circle_infra'
 WHERE platform IS NULL
   AND (url ~* '^https?://(app|login|www|help|status|signup|auth|assets|cdn|marketing|compass|email|mail)\.circle\.so'
        OR url = 'https://discover.circle.so'
        -- multi-label subdomain (e.g. email.notification.circle.so): not a plain community
        OR (url ~* '\.circle\.so' AND url !~* '^https?://[a-z0-9-]+\.circle\.so($|/)'));

-- Plain <slug>.circle.so communities
UPDATE communities SET platform = 'circle'
 WHERE platform IS NULL AND url ~* '^https?://[a-z0-9-]+\.circle\.so($|/)';

-- Custom-domain Circle communities (each verified: /internal_api/spaces -> JSON)
UPDATE communities SET platform = 'circle'
 WHERE platform IS NULL
   AND url IN ('https://forum.joelpilger.com',
               'https://www.siliconslopes.com',
               'https://www.yourspinstate.com',
               'https://community.bigstarlights.com');

-- Everything else that isn't an http(s) community URL
UPDATE communities SET platform = 'other'
 WHERE platform IS NULL AND url NOT ILIKE 'http%';

-- Verify: expect circle≈102, discover 32, circle_infra 3, other 1, NULL 0
--   SELECT platform, count(*) FROM communities GROUP BY platform ORDER BY 2 DESC;

COMMIT;
