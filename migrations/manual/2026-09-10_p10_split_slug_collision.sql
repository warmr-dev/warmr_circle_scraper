-- P10 — split the www / community slug-collision rows into real communities
-- ============================================================================
-- Problem: circle_leads/scanning.py derived a community slug as
--   host.split(".")[0]
-- so every private cookie scan of a custom-domain community collapsed onto one
-- of two junk rows:
--
--   communities.id = 649  slug 'www'        (name www.templeofplantspiritmedicine.com)
--     852 posts from www.siliconslopes.com
--      68 posts from www.yourspinstate.com
--   communities.id = 644  slug 'community'  (name community.maikerhub.com)
--     101 posts from community.bigstarlights.com
--
-- Verified before writing this migration:
--   * every post in 644/649 has url = exactly one of the three community base
--     URLs (no per-post permalinks yet), so re-pointing by url is unambiguous
--   * 0 posts in 644/649 reference an author row outside 644/649
--   * 0 authors in 644/649 are referenced by posts outside 644/649
--   * 0 authors in 644/649 have posts spanning more than one of the 3 hosts
--   * 3 of the 15 leads sit on posts in 644/649 (they follow the posts, FK is
--     leads.post_id)
--
-- The code fix ships in the same PR (community_slug_for_host in
-- circle_leads/discovery/discover_communities.py). Even without it, the next
-- scan is safe because get_or_create_community also matches on the unique url.
--
-- Backup taken first: data/migration_backups/<ts>/ (communities_644_649.json,
-- posts_644_649.json, authors_644_649.json, leads_on_644_649.json).
--
-- Run inside one transaction. Re-runnable: the INSERT is guarded by NOT EXISTS.
-- ============================================================================

BEGIN;

-- 1. Real community rows (idempotent).
INSERT INTO communities (slug, name, url, discovered_at, discovery_source,
                         access_status, permission_status, relevance_score,
                         relevant, watching, updated_at)
SELECT v.slug, v.name, v.url, now(), 'member_session',
       'visited', 'candidate', 0, true, false, now()
FROM (VALUES
        ('siliconslopes', 'Silicon Slopes',              'https://www.siliconslopes.com'),
        ('yourspinstate', 'Spin State Operating System',  'https://www.yourspinstate.com'),
        ('bigstarlights', 'Big Star Lights Community',     'https://community.bigstarlights.com')
     ) AS v(slug, name, url)
WHERE NOT EXISTS (SELECT 1 FROM communities c WHERE c.url = v.url);

-- 2. Re-point posts by their (unique-per-host) url.
UPDATE posts SET community_id = (SELECT id FROM communities WHERE url = 'https://www.siliconslopes.com')
 WHERE community_id = 649 AND url = 'https://www.siliconslopes.com';

UPDATE posts SET community_id = (SELECT id FROM communities WHERE url = 'https://www.yourspinstate.com')
 WHERE community_id = 649 AND url = 'https://www.yourspinstate.com';

UPDATE posts SET community_id = (SELECT id FROM communities WHERE url = 'https://community.bigstarlights.com')
 WHERE community_id = 644 AND url = 'https://community.bigstarlights.com';

-- 3. Move authors to the community their posts now live in. Each author has
--    posts from exactly one of the 3 hosts, so this is unambiguous.
UPDATE authors a
   SET community_id = p.community_id
  FROM (SELECT DISTINCT author_id, community_id
          FROM posts
         WHERE author_id IS NOT NULL
           AND community_id IN (SELECT id FROM communities
                                 WHERE url IN ('https://www.siliconslopes.com',
                                               'https://www.yourspinstate.com',
                                               'https://community.bigstarlights.com'))
       ) p
 WHERE a.id = p.author_id
   AND a.community_id IN (644, 649);

-- 4. Authors still stranded on 644/649 have zero posts anywhere (dedup cruft).
DELETE FROM authors a
 WHERE a.community_id IN (644, 649)
   AND NOT EXISTS (SELECT 1 FROM posts p WHERE p.author_id = a.id);

-- 5. The junk community rows must now be empty. This fails the migration if
--    anything is still attached (safety check).
DELETE FROM communities c
 WHERE c.id IN (644, 649)
   AND NOT EXISTS (SELECT 1 FROM posts   p  WHERE p.community_id  = c.id)
   AND NOT EXISTS (SELECT 1 FROM authors au WHERE au.community_id = c.id)
   AND NOT EXISTS (SELECT 1 FROM spaces  sp WHERE sp.community_id = c.id)
   AND NOT EXISTS (SELECT 1 FROM circle_connections cc WHERE cc.community_id = c.id);

-- 6. Verify (should return 3 rows, 0 orphans, 2 deleted junk rows gone).
--    Comment out COMMIT and run these first if you want to eyeball it.
--   SELECT id, slug, url,
--          (SELECT count(*) FROM posts   WHERE community_id = communities.id) AS posts,
--          (SELECT count(*) FROM authors WHERE community_id = communities.id) AS authors
--     FROM communities WHERE url IN ('https://www.siliconslopes.com',
--                                    'https://www.yourspinstate.com',
--                                    'https://community.bigstarlights.com');
--   SELECT count(*) AS junk_rows_left FROM communities WHERE id IN (644, 649);

COMMIT;
