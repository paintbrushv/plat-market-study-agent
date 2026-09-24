"""DuckDB connection + schema migration helpers."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import duckdb

SCHEMA_SQL_PATH = Path(__file__).parent / "schema.sql"
MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def connect(db_path: Path) -> duckdb.DuckDBPyConnection:
    """Open (or create) a DuckDB connection at db_path."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return duckdb.connect(str(db_path))


def apply_schema(conn: duckdb.DuckDBPyConnection) -> None:
    """Apply the bootstrap schema and any pending numbered migrations.

    Idempotent: safe to call on a fresh DB or one that's already up to date.
    """
    sql = SCHEMA_SQL_PATH.read_text(encoding="utf-8")
    conn.execute(sql)
    _apply_pending_migrations(conn)


def _apply_pending_migrations(conn: duckdb.DuckDBPyConnection) -> None:
    applied = {
        row[0]
        for row in conn.execute("SELECT migration_id FROM _schema_migrations").fetchall()
    }
    if not MIGRATIONS_DIR.exists():
        return
    for path in sorted(MIGRATIONS_DIR.glob("[0-9][0-9][0-9]_*.sql")):
        if path.stem in applied:
            continue
        conn.execute(path.read_text(encoding="utf-8"))
        conn.execute(
            "INSERT INTO _schema_migrations VALUES (?, ?)",
            [path.stem, dt.datetime.now(dt.UTC)],
        )
