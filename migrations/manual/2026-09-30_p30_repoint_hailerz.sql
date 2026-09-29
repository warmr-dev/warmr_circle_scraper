-- Re-point hailerz (#2380) to the Circle host it lives at now.
--
-- community.hailerz.com no longer serves the community: its DNS points to
-- porkbun, not Circle, and the feed answers 301 to www.hailerz.com. The
-- watcher failed there 150 times in a row (2026-09-23..29), and the row sat in
-- the join queue at an address the bot cannot join through. p27 switched it
-- off as "left Circle" but was never applied.
--
-- It has not left: on 2026-09-30 hailerz.circle.so answered
-- /internal_api/communities/current with 200 {"slug": "hailerz", "name":
-- "Hailerz Global Talent", "is_private": false} (an invented slug answers
-- 401), and its home feed showed 3 posts from 2026-09-29.
--
-- Safe to rewrite in place: the row has 0 posts, 0 leads, no session and no
-- connection, and no row holds hailerz.circle.so. Row #13618
-- (www.hailerz.com, not ICP-fit) is the company site, a different row, and
-- stays as it is.
--
-- The other rows of p27's list need nothing by hand: the watcher now parks a
-- domain that keeps redirecting away or failing TLS after five tries.

BEGIN;

-- Must return no rows. If it returns one, stop: that row already holds the host.
SELECT id, slug FROM communities WHERE host = 'hailerz.circle.so';

UPDATE communities
SET url = 'https://hailerz.circle.so', host = 'hailerz.circle.so'
WHERE id = 2380 AND host = 'community.hailerz.com';

UPDATE watch_state
SET host = 'hailerz.circle.so',
    mode = 'anon',
    consecutive_errors = 0,
    last_status = NULL,
    last_detail = 'repointed to the circle.so host (p30)',
    next_check_at = now()
WHERE community_id = 2380;

COMMIT;

-- Check: one row, host hailerz.circle.so, watch row anon with 0 errors.
-- SELECT c.id, c.host, c.url, w.host, w.mode, w.consecutive_errors
-- FROM communities c JOIN watch_state w ON w.community_id = c.id WHERE c.id = 2380;
--
-- Rollback:
-- UPDATE communities SET url = 'https://community.hailerz.com/about?utm_source=circle_discover',
--   host = 'community.hailerz.com' WHERE id = 2380;
-- UPDATE watch_state SET host = 'community.hailerz.com' WHERE community_id = 2380;
