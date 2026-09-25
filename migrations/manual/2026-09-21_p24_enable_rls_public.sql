-- p24 — Row Level Security on every table in `public`.
--
-- Supabase exposes `public` through PostgREST: with RLS off, anyone holding the
-- project's anon/publishable key can read and write every row over
-- https://<ref>.supabase.co/rest/v1/ — leads, circle_connections, and the
-- (encrypted) session cookies in replay_sessions.
--
-- RLS on + no policies = anon/authenticated get nothing. The dashboard, worker
-- and desktop app connect with CIRCLE_LEADS_DB as `postgres`, the owner of these
-- tables, which bypasses RLS (not FORCEd) — nothing changes for them.
-- `warmr_app` (desktop) has no anon/authenticated grants and is left alone.
--
-- Run the two parts as separate queries in the Supabase SQL Editor. Part 1 is
-- the fix; part 2 only keeps future tables covered. The first attempt (both
-- parts in one query, trigger tag list incl. the CREATE-TABLE-AS tag) failed in
-- the SQL Editor with `42P01: relation "AS" does not exist` and rolled back; the
-- same script runs clean on plain Postgres. The app only ever issues plain
-- CREATE TABLE (SQLAlchemy create_all), so part 2 filters on that tag alone.
--
-- Idempotent. Rollback: `alter table public.<t> disable row level security;`
-- and `drop event trigger rls_auto_enable; drop function public.rls_auto_enable();`

-- ── Part 1: existing tables ────────────────────────────────────────────────
do $$
declare t record;
begin
  for t in
    select c.oid::regclass as tbl
    from pg_class c join pg_namespace n on n.oid = c.relnamespace
    where n.nspname = 'public' and c.relkind in ('r', 'p') and not c.relrowsecurity
  loop
    execute format('alter table %s enable row level security', t.tbl);
  end loop;
end $$;

-- Check: every row should be rls_on = true, owner = postgres.
select c.relname as table_name, c.relrowsecurity as rls_on, pg_get_userbyid(c.relowner) as owner
from pg_class c join pg_namespace n on n.oid = c.relnamespace
where n.nspname = 'public' and c.relkind in ('r', 'p')
order by 1;

-- ── Part 2: tables created later (create_all() on worker/dashboard boot) ───
create or replace function public.rls_auto_enable() returns event_trigger
language plpgsql security definer set search_path = pg_catalog as $$
declare cmd record;
begin
  for cmd in
    select * from pg_event_trigger_ddl_commands()
    where command_tag = 'CREATE TABLE'
      and object_type in ('table', 'partitioned table')
      and schema_name = 'public'
  loop
    execute format('alter table if exists %s enable row level security', cmd.object_identity);
  end loop;
end $$;

drop event trigger if exists rls_auto_enable;
create event trigger rls_auto_enable on ddl_command_end
  when tag in ('CREATE TABLE')
  execute function public.rls_auto_enable();

-- Check: one row, rls_auto_enable, enabled = O.
select evtname, evtevent, evtenabled from pg_event_trigger where evtname = 'rls_auto_enable';
