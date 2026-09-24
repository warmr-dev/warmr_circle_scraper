create schema if not exists warmr_app;

-- P22 (2026-09-18): schema for the desktop app (desktop/, Warmr Circle).
--
-- Applied automatically by the app on first connect (desktop/src/engine/db/migrations.ts,
-- version '2026-09-18_001_desktop_app_schema', recorded in warmr_app.schema_migrations).
-- This copy is for the record and for applying by hand if ever needed.
--
-- Everything lives in a separate schema: no public.* column is added or changed,
-- so the Python worker/dashboard keep working unchanged. Supabase exposes only
-- `public` through PostgREST and a fresh schema has no anon/authenticated grants.
--
-- Rollback: drop schema warmr_app cascade;

create table if not exists warmr_app.schema_migrations (
  version text primary key,
  applied_at timestamp not null default (now() at time zone 'utc')
);


create table if not exists warmr_app.devices (
  id text primary key,
  name text,
  platform text,
  app_version text,
  current_stage text,
  last_seen_at timestamp not null default (now() at time zone 'utc')
);

create table if not exists warmr_app.settings (
  key text primary key,
  value jsonb not null,
  updated_by text,
  updated_at timestamp not null default (now() at time zone 'utc')
);

create table if not exists warmr_app.locks (
  name text primary key,
  holder text not null,
  expires_at timestamp not null
);

create table if not exists warmr_app.runs (
  id bigserial primary key,
  stage text not null,
  device_id text,
  started_at timestamp not null default (now() at time zone 'utc'),
  finished_at timestamp,
  ok boolean,
  summary text,
  counts jsonb,
  error text
);
create index if not exists runs_stage_started_idx on warmr_app.runs (stage, started_at desc);

create table if not exists warmr_app.community_state (
  community_id integer primary key references public.communities(id) on delete cascade,
  exists_status text,
  probed_at timestamp,
  probe_host text,
  probe_http integer,
  probe_detail text,
  circle_community_id bigint,
  is_private boolean,
  allow_signups boolean,
  has_paywalls boolean,
  spaces_public integer,
  space_names text[],
  posts_public integer,
  members_total integer,
  icp_version text,
  icp_at timestamp,
  icp_rule_score integer,
  icp_llm_model text,
  icp_llm_fit boolean,
  icp_llm_score integer,
  icp_llm_confidence real,
  icp_llm_reason text,
  directory_resolved_at timestamp,
  directory_resolve_detail text,
  last_scraped_at timestamp,
  scrape_state text,
  scrape_detail text,
  posts_stored integer not null default 0,
  updated_at timestamp not null default (now() at time zone 'utc')
);
create index if not exists community_state_exists_idx on warmr_app.community_state (exists_status);
create index if not exists community_state_probed_idx on warmr_app.community_state (probed_at);

create table if not exists warmr_app.join_attempts (
  id bigserial primary key,
  community_id integer references public.communities(id) on delete set null,
  host text,
  url text,
  account text not null,
  status text not null,
  terminal boolean not null,
  detail text,
  device_id text,
  created_at timestamp not null default (now() at time zone 'utc')
);
create index if not exists join_attempts_account_day_idx on warmr_app.join_attempts (account, created_at);
create index if not exists join_attempts_community_idx on warmr_app.join_attempts (community_id);

create table if not exists warmr_app.tasks (
  id bigserial primary key,
  kind text not null,
  community_id integer references public.communities(id) on delete cascade,
  host text,
  title text not null,
  detail text,
  state text not null default 'open',
  created_at timestamp not null default (now() at time zone 'utc'),
  resolved_at timestamp
);
create index if not exists tasks_state_idx on warmr_app.tasks (state, created_at desc);
create unique index if not exists tasks_one_open_per_kind on warmr_app.tasks (kind, coalesce(community_id, 0), coalesce(host, '')) where state = 'open';

create table if not exists warmr_app.space_sync (
  community_id integer not null references public.communities(id) on delete cascade,
  source_space_id text not null,
  space_pk integer references public.spaces(id) on delete set null,
  name text,
  space_type text,
  newest_seen_at timestamp,
  backfill_done boolean not null default false,
  backfill_next_page integer not null default 1,
  posts_seen integer not null default 0,
  last_read_at timestamp,
  state text,
  detail text,
  primary key (community_id, source_space_id)
);

create table if not exists warmr_app.post_meta (
  post_id integer primary key references public.posts(id) on delete cascade,
  community_id integer not null,
  source_post_id text not null,
  comments_count integer,
  comments_fetched_count integer,
  comments_fetched_at timestamp
);
create index if not exists post_meta_community_idx on warmr_app.post_meta (community_id);

create table if not exists warmr_app.llm_calls (
  id bigserial primary key,
  purpose text not null,
  community_id integer,
  provider text not null,
  model text not null,
  input_tokens integer,
  output_tokens integer,
  usd numeric(12, 6),
  ok boolean not null,
  error text,
  created_at timestamp not null default (now() at time zone 'utc')
);
create index if not exists llm_calls_created_idx on warmr_app.llm_calls (created_at);

insert into warmr_app.schema_migrations (version) values ('2026-09-18_001_desktop_app_schema') on conflict do nothing;
