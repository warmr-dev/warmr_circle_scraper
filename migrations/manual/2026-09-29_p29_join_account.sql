-- p29: which of our Circle accounts got us into a community.
--
-- The user, 2026-09-29: "keep which account we got in with, and refresh the
-- cookies from that account". Only a stored session carried a label
-- (replay_sessions.member_label), and only when cookies were captured: 16 of
-- the 42 joined communities have no session at all. The label was also wiped
-- every time the extension sent fresh cookies for a host.
--
--   communities.join_account: an account key from circle_leads/join/accounts.py
--   (main, test, 3..10) or a name a person gave (client). NULL = not recorded.
--
-- Additive only: the running code does not map it, so applying this before the
-- deploy changes nothing. It MUST be applied before any code that maps it runs
-- anywhere -- the worker and Vercel both run with SKIP_DB_INIT=true, and the
-- ORM selects every mapped column of `communities`.
--
-- Idempotent. Rollback (only after the code that maps it is gone everywhere):
--   alter table communities drop column if exists join_account;

begin;
alter table communities add column if not exists join_account varchar(32);
commit;

-- Backfill: the accounts that are on record. The bot joins come from the Mac's
-- join log (~/Library/Application Support/warmr/worker/data/join_attempts.log,
-- status "joined", its "account" field); thefpahub and b2b-tactics say in their
-- own join_status_detail that they were joined "under the erkshtest account",
-- which is account 4. Nothing else is guessed: the 20 joins of 2026-09-15/16
-- and foundercareers ("bot erksh3 (+3)") stay NULL until a person sets them on
-- the dashboard. Touches only joined rows that have no account yet.
begin;
update communities as c
set join_account = v.account
from (values
  ('agencybuilders', 'main'),
  ('asaporg', 'main'),
  ('ceebeedee', 'main'),
  ('designedforward', 'main'),
  ('kiwitech', 'main'),
  ('mvp', 'main'),
  ('startupsnl', 'main'),
  ('the-technical-freelancer-academy', 'main'),
  ('building-a-second-brain', 'test'),
  ('bumbleb', 'test'),
  ('circle-for-impact', 'test'),
  ('future-of-saas', 'test'),
  ('launchthedamnthing', 'test'),
  ('roblox', 'test'),
  ('www.rhombus.community', 'test'),
  ('thefpahub', '4'),
  ('b2b-tactics', '4')
) as v(slug, account)
where c.slug = v.slug
  and c.join_status = 'joined'
  and c.join_account is null;
commit;

-- Check: 17 rows should carry an account, the other joined ones NULL.
select coalesce(join_account, '(not recorded)') as account, count(*)
from communities
where join_status = 'joined'
group by 1
order by 2 desc;
