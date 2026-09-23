-- Re-point 13 communities whose custom domain no longer serves them.
--
-- Found by the watcher: 10 hosts failed the TLS handshake identically
-- (SSLV3_ALERT_HANDSHAKE_FAILURE from two different machines and TLS stacks),
-- and 3 answered 301. DNS shows why: each custom domain is still a CNAME to
-- its *.circle.so host, but Circle no longer holds a certificate for the
-- custom name. The community itself is alive at the Circle host.
--
-- Safe to rewrite in place: every one of these rows has 0 posts and 0 leads,
-- no row exists yet at the target host, and each row's slug already matches
-- the Circle subdomain -- so this cannot create the duplicate-community
-- problem that p25 was written to prevent.
--
-- Reading the target host: 1 returns the feed (forumnocodespace), 10 return
-- 401 (members only), 2 have left Circle entirely.

BEGIN;

-- Alive at a Circle host.
UPDATE communities SET url = 'https://' || v.new_host, host = v.new_host
FROM (VALUES
  (6473,  'dpub.circle.so'),
  (9485,  'nocoder.circle.so'),
  (9471,  'no-code-switzerland.circle.so'),
  (9297,  'mustangs-club.circle.so'),
  (7108,  'forumnocodespace.circle.so'),
  (7236,  'future-of-saas.circle.so'),
  (5873,  'codesandbox.circle.so'),
  (6853,  'expa.circle.so'),
  (9206,  'momentumlifestyle.circle.so'),
  (7126,  'founderhood.circle.so'),
  (10487, 'saasclub.circle.so')
) AS v(id, new_host)
WHERE communities.id = v.id;

UPDATE watch_state SET host = c.host,
                       consecutive_errors = 0,
                       last_status = NULL,
                       last_detail = 'repointed to the circle.so host (p27)',
                       next_check_at = now()
FROM communities c
WHERE watch_state.community_id = c.id
  AND c.id IN (6473,9485,9471,9297,7108,7236,5873,6853,9206,7126,10487);

-- Gone from Circle: community.hailerz.com -> www.hailerz.com (403, not Circle),
-- edu.galileoxp.com -> kubrio.com (200 HTML, not Circle). Stop polling them
-- every cycle; the daily off-mode check is enough to notice if they return.
UPDATE watch_state SET mode = 'off',
                       consecutive_errors = 0,
                       last_detail = 'left Circle: host redirects off-platform (p27)',
                       next_check_at = now() + interval '1 day'
WHERE community_id IN (2380, 11309);

COMMIT;
