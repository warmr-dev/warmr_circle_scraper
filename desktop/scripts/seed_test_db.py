"""Build a DISPOSABLE local test database for desktop/tests/integration.test.ts.

Schema: created by the Python app's own models (circle_leads.storage.Database),
so the desktop engine is tested against exactly what the Python side expects.
Rows: a small read-only sample from the database in CIRCLE_LEADS_DB (.env),
including two member sessions (cookies) for the scrape test.

The copy holds live session cookies: keep it local and drop it when done
(pg_ctl stop; rm -rf the data dir).

Usage (from the repo root, with a Postgres listening on 127.0.0.1:55432):
  createdb -h 127.0.0.1 -p 55432 -U postgres warmr_test
  .venv/bin/python desktop/scripts/seed_test_db.py
"""
import os
import sys

sys.path.insert(0, os.getcwd())
for line in open(".env"):
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        os.environ.setdefault(k, v.strip().strip('"').strip("'"))

LOCAL = os.environ.get("WARMR_TEST_DB", "postgresql://postgres@127.0.0.1:55432/warmr_test")
if "supabase" in LOCAL:
    sys.exit("refusing: WARMR_TEST_DB must be a disposable local database")

from circle_leads.storage.database import Database  # noqa: E402
import psycopg  # noqa: E402
from psycopg.types.json import Json  # noqa: E402

Database(LOCAL)  # create_all + _ensure_columns, exactly like the Python app
prod = psycopg.connect(os.environ["CIRCLE_LEADS_DB"].replace("postgresql+psycopg://", "postgresql://"), prepare_threshold=None)
prod.execute("set default_transaction_read_only = on")
local = psycopg.connect(LOCAL, autocommit=True)


def copy(table, where, params=()):
    local_cols = [r[0] for r in local.execute(
        "select column_name from information_schema.columns where table_schema='public' and table_name=%s order by ordinal_position",
        (table,)).fetchall()]
    prod_cols = {r[0] for r in prod.execute(
        "select column_name from information_schema.columns where table_schema='public' and table_name=%s", (table,)).fetchall()}
    use = [c for c in local_cols if c in prod_cols]
    types = {r[0]: r[1] for r in local.execute(
        "select column_name, data_type from information_schema.columns where table_schema='public' and table_name=%s",
        (table,)).fetchall()}
    n = 0
    for row in prod.execute(f"select {', '.join(use)} from public.{table} where {where}", params).fetchall():
        vals = [Json(v) if types[c] in ("json", "jsonb") and v is not None else v for c, v in zip(use, row)]
        local.execute(
            f"insert into public.{table} ({', '.join(use)}) values ({', '.join(['%s'] * len(use))}) on conflict do nothing", vals)
        n += 1
    return n


hosts = ["circle-for-impact.circle.so", "community.id.vc"]
ids = set()
for where in [
    "platform='circle' and icp_reasons::text like '%no_metadata%' order by id desc limit 40",
    "icp_flag and join_type='free_join' and coalesce(join_status,'not_attempted')='not_attempted' order by icp_score desc limit 20",
    "platform='discover' order by id desc limit 20",
    "platform='circle' and join_type='locked_unknown' and id % 97 = 3 limit 25",
    "platform='circle' and join_type='unknown' and id % 89 = 5 limit 25",
    "platform='circle' and join_type in ('free_join','paid') and name is not null and id % 23 = 1 limit 25",
]:
    ids.update(r[0] for r in prod.execute(f"select id from public.communities where {where}").fetchall())
ids.update(r[0] for r in prod.execute("select id from public.communities where url = any(%s)", (["https://" + h for h in hosts],)).fetchall())
print("communities:", copy("communities", "id = any(%s)", (list(ids),)))
print("replay_sessions:", copy("replay_sessions", "host = any(%s)", (hosts,)))
print("circle_connections:", copy("circle_connections", "host = any(%s)", (hosts,)))
print("settings:", copy("settings", "key in ('harvest_schedule','harvest_search','icp_classification_schedule','harvest_last_run')"))
for table in ("communities", "replay_sessions", "circle_connections"):
    local.execute(f"select setval(pg_get_serial_sequence('public.{table}','id'), (select max(id) from public.{table}))")
