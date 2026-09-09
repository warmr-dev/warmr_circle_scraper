-- P11 — clean up the unvetted urlscan community import
-- ============================================================================
-- Someone bulk-loaded communities from urlscan.io exports (discovery_source
-- 'csv:urlscan' and 'jsonl:urlscan') and left them all watching=true without
-- vetting. Two problems:
--
--   a) 29 'jsonl:urlscan' rows are not Circle communities at all -- they are
--      unrelated custom domains urlscan happened to associate with a circle.so
--      asset (gimbalgod.com, billing-intelligence.com, satyaspeaks.co, ...).
--      No posts, no authors, never usable. Delete them.
--
--   b) 86 'csv:'/'jsonl:urlscan' rows are on the harvest watchlist but have
--      never produced a lead (mostly relevance_score 0, names like "Oops! The
--      page was not found" and SEO spam). They crowd the harvest fast lane.
--      Un-watch them -- keep the rows (and any posts) so nothing is lost, and
--      re-watch individually if one proves useful.
--
-- Kept on the watchlist: everything from harvest:* / search-watch:* discovery
-- (auto-discovered and scored), and the 1 urlscan row that did yield a lead.
--
-- Run P10 first (it removes junk rows 644/649). Backup: the full communities
-- table is in data/migration_backups/<ts>/communities_all.json.
--
-- Predicate-based so it stays correct even if row ids shifted. Expected counts
-- from the 2026-09-10 snapshot: delete 29, unwatch 86.
--
-- id lists at snapshot time (cross-check):
--   DELETE  647 648 651 652 653 654 655 656 657 658 659 660 661 662 663 664
--           665 666 667 668 670 671 672 673 674 675 676 677 678
--   UNWATCH 300 307 319 320 321 328 334 337 340 341 346 347 348 350 358 365
--           368 369 374 379 382 383 392 396 397 399 417 418 420 427 430 431
--           432 434 435 439 452 454 456 457 458 459 460 464 465 472 477 489
--           491 492 493 500 503 508 528 530 537 540 546 547 551 562 564 569
--           574 575 581 583 593 594 604 605 606 609 612 617 619 620 621 622
--           625 637 641 643 645 650
-- ============================================================================

BEGIN;

-- a) Delete non-Circle jsonl:urlscan rows that hold nothing.
DELETE FROM communities c
 WHERE c.discovery_source = 'jsonl:urlscan'
   AND c.url !~* '\.circle\.so'
   AND c.id NOT IN (644, 649)                       -- handled by P10; guard anyway
   AND NOT EXISTS (SELECT 1 FROM posts   p  WHERE p.community_id  = c.id)
   AND NOT EXISTS (SELECT 1 FROM authors au WHERE au.community_id = c.id)
   AND NOT EXISTS (SELECT 1 FROM circle_connections cc WHERE cc.community_id = c.id);

-- b) Un-watch unvetted urlscan rows that never produced a lead.
UPDATE communities c
   SET watching = false, updated_at = now()
 WHERE c.watching
   AND c.discovery_source IN ('csv:urlscan', 'jsonl:urlscan')
   AND NOT EXISTS (
         SELECT 1 FROM leads l JOIN posts p ON p.id = l.post_id
          WHERE p.community_id = c.id
       );

-- Verify:
--   SELECT discovery_source, count(*) FILTER (WHERE watching) AS watching, count(*)
--     FROM communities GROUP BY discovery_source ORDER BY 2 DESC;

COMMIT;
