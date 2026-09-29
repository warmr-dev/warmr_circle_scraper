-- p28: what the new dashboard needs that the database did not keep.
--
-- 1. Vini's answer per lead. `external_synced_at` was the only trace of a
--    push, and it is set both for a lead Vini published and for one it parked
--    (the client never sees a parked lead). The reasons lived in the worker's
--    log and in Telegram only, so "which leads did Vini take, and why not the
--    rest" had no answer on the dashboard.
--    vini_status: accepted | duplicate | held | rejected | error
--    vini_reason: Vini's own words (decision + holdReason, or the error)
--    vini_ref:    the id Vini returned for its row, when it returns one
--
-- 2. attention_acks: an operator's "seen it" on one item of the dashboard's
--    attention list. The fingerprint is the state the item was in; the item
--    comes back when that state changes, so a "seen it" cannot hide a new
--    problem on the same community.
--
-- Additive only: the running code maps neither, so applying this before the
-- deploy changes nothing. It MUST be applied before any code that maps these
-- columns runs anywhere -- the worker and Vercel both run with
-- SKIP_DB_INIT=true, and the ORM selects every mapped column of `leads`.
--
-- Idempotent. Rollback (only after the code that maps them is gone
-- everywhere):
--   drop table if exists attention_acks;
--   alter table leads drop column if exists vini_status,
--     drop column if exists vini_reason, drop column if exists vini_ref,
--     drop column if exists vini_attempts,
--     drop column if exists vini_last_attempt_at,
--     drop column if exists vini_responded_at;

begin;

alter table leads add column if not exists vini_status varchar(32);
alter table leads add column if not exists vini_reason text;
alter table leads add column if not exists vini_ref varchar(64);
alter table leads add column if not exists vini_attempts integer not null default 0;
alter table leads add column if not exists vini_last_attempt_at timestamp;
alter table leads add column if not exists vini_responded_at timestamp;
create index if not exists ix_leads_vini_status on leads (vini_status);

create table if not exists attention_acks (
    rule         varchar(64)  not null,
    item_key     varchar(255) not null,
    fingerprint  varchar(64)  not null,
    note         text,
    acked_at     timestamp    not null default (now() at time zone 'utc'),
    primary key (rule, item_key)
);
-- p24's event trigger already does this for new tables; stated here so the
-- file is right on its own.
alter table attention_acks enable row level security;

commit;

-- Check: six vini_* columns, and the table with rls_on = true.
select column_name, data_type, is_nullable, column_default
from information_schema.columns
where table_schema = 'public' and table_name = 'leads' and column_name like 'vini\_%'
order by column_name;

select c.relname as table_name, c.relrowsecurity as rls_on, pg_get_userbyid(c.relowner) as owner
from pg_class c join pg_namespace n on n.oid = c.relnamespace
where n.nspname = 'public' and c.relname = 'attention_acks';
