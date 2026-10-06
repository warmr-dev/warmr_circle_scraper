-- Apply separately before deploying code that maps these columns.
-- No data backfill, reset, reclassification or historical export.
begin;
alter table posts add column if not exists classification_audit json;
alter table posts add column if not exists classification_retry_at timestamp;
create index if not exists ix_posts_classification_retry_at on posts (classification_retry_at);
commit;
