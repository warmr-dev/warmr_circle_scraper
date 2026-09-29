"""The p28 columns exist in all three places that must agree.

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
from circle_leads.storage.models import AttentionAck, Lead

MIGRATION = Path(__file__).resolve().parents[1] / "migrations/manual/2026-09-29_p28_dashboard.sql"
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
