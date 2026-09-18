-- Desktop app (desktop/src/engine/db/migrations.ts, version
-- 2026-09-18_002_post_meta_kind). Applied automatically by the app on connect;
-- this copy is for the record.
--
-- The app now stores posts and comments under Python's content key
-- ("triage:" + content_hash[:24], content_type 'post'), the same key the old
-- worker uses, so the two never write one post twice. What an item is (post
-- or comment) and which post a comment belongs to live in warmr_app.post_meta.

alter table warmr_app.post_meta add column if not exists kind text;
alter table warmr_app.post_meta add column if not exists parent_source_id text;
