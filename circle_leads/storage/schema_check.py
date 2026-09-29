"""Does the database have every column the code maps?

Production runs with SKIP_DB_INIT=true: the code never adds its own columns,
the manual migrations in migrations/manual/ do. A release that maps a column
the database lacks does not fail on start -- it fails on the first query that
selects the table, which is every query (read_outcome, 2026-09-23). So the
deploy asks this before it switches the release.
"""

from __future__ import annotations

from sqlalchemy import inspect

from circle_leads.storage.models import Base


def missing_columns(engine) -> list[str]:
    """``table.column`` for every mapped column the database does not have
    (``table`` alone for a missing table). Reads the catalogue only."""
    catalogue = inspect(engine)
    present = set(catalogue.get_table_names())
    missing: list[str] = []
    for table in Base.metadata.sorted_tables:
        if table.name not in present:
            missing.append(table.name)
            continue
        have = {c["name"] for c in catalogue.get_columns(table.name)}
        missing += [f"{table.name}.{c.name}" for c in table.columns if c.name not in have]
    return missing
