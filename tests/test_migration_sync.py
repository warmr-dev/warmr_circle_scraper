"""The p28 and p29 columns exist in all three places that must agree.

Production runs with SKIP_DB_INIT=true, so a column the model maps but the
manual migration forgets breaks every query on `leads` there, while the tests
(SQLite, create_all) stay green. And an older SQLite database only gains the
column through _ensure_columns.
"""
from __future__ import annotations

import re
import sqlite3
from pathlib import Path

from sqlalchemy import inspect

from circle_leads.storage.database import Database
from circle_leads.storage.models import AttentionAck, Community, Lead

MIGRATION = Path(__file__).resolve().parents[1] / "migrations/manual/2026-09-29_p28_dashboard.sql"
P29 = Path(__file__).resolve().parents[1] / "migrations/manual/2026-09-29_p29_join_account.sql"
VINI_COLUMNS = sorted(c.name for c in Lead.__table__.columns if c.name.startswith("vini_"))


def test_the_model_has_the_vini_columns():
    assert VINI_COLUMNS == ["vini_attempts", "vini_last_attempt_at", "vini_reason",
                            "vini_ref", "vini_responded_at", "vini_status"]


def test_the_migration_adds_every_vini_column():
    sql = MIGRATION.read_text()
    added = set(re.findall(r"add column if not exists (vini_\w+)", sql))
    assert added == set(VINI_COLUMNS)


def test_the_migration_creates_the_ack_table_with_the_model_columns():
    sql = MIGRATION.read_text()
    block = sql.split("create table if not exists attention_acks", 1)[1].split(");", 1)[0]
    for column in AttentionAck.__table__.columns:
        assert re.search(rf"\b{column.name}\b", block), column.name
    assert "enable row level security" in sql


def test_an_old_sqlite_database_gains_the_columns(tmp_path):
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE leads (id INTEGER PRIMARY KEY, post_id INTEGER)")
    db = Database(f"sqlite:///{path}")
    columns = {c["name"] for c in inspect(db.engine).get_columns("leads")}
    assert set(VINI_COLUMNS) <= columns
    assert "attention_acks" in inspect(db.engine).get_table_names()


def test_p29_adds_the_join_account_column_the_model_maps():
    assert "join_account" in Community.__table__.columns
    sql = P29.read_text()
    assert "alter table communities add column if not exists join_account varchar(32)" in sql
    assert Community.__table__.columns["join_account"].type.length == 32


def test_the_p29_backfill_only_fills_joined_rows_without_an_account():
    sql = P29.read_text()
    update = sql.split("update communities", 1)[1].split("commit;", 1)[0]
    assert "c.join_status = 'joined'" in update
    assert "c.join_account is null" in update


def test_an_old_sqlite_database_gains_the_join_account(tmp_path):
    path = tmp_path / "old29.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE communities (id INTEGER PRIMARY KEY, slug TEXT, url TEXT)")
    db = Database(f"sqlite:///{path}")
    assert "join_account" in {c["name"] for c in inspect(db.engine).get_columns("communities")}


def test_check_schema_names_what_a_database_lacks(tmp_path, monkeypatch):
    """The deploy's guard: a release must not switch onto a database without
    its migrations (SKIP_DB_INIT=true never adds a column by itself)."""
    from click.testing import CliRunner

    from circle_leads.cli.main import cli
    from circle_leads.storage.schema_check import missing_columns

    path = tmp_path / "full.db"
    full = Database(f"sqlite:///{path}")
    assert missing_columns(full.engine) == []

    with sqlite3.connect(path) as conn:
        conn.execute("ALTER TABLE communities DROP COLUMN join_account")
    monkeypatch.setenv("SKIP_DB_INIT", "true")
    result = CliRunner().invoke(cli, ["--db", f"sqlite:///{path}", "check-schema"])
    assert result.exit_code == 1
    assert "communities.join_account" in result.output
