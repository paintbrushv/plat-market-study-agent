from __future__ import annotations

import datetime as dt
from pathlib import Path

import duckdb
from etl.gainesville.db import apply_schema, connect
from etl.gainesville.run_id import next_run_id


def _seed(conn: duckdb.DuckDBPyConnection, run_id: str) -> None:
    conn.execute(
        "INSERT INTO run_log VALUES (?, ?, ?, 'success', [], [], 0, 0, 0, '')",
        [run_id, dt.datetime.now(dt.UTC), dt.datetime.now(dt.UTC)],
    )


def test_first_run_of_week_no_suffix(tmp_path: Path) -> None:
    conn = connect(tmp_path / "t.duckdb")
    apply_schema(conn)
    rid = next_run_id(conn, dt.date(2026, 5, 4))  # Mon of W19
    assert rid == "2026-W19_gainesville"


def test_second_run_of_week_appends_r2(tmp_path: Path) -> None:
    conn = connect(tmp_path / "t.duckdb")
    apply_schema(conn)
    _seed(conn, "2026-W19_gainesville")
    rid = next_run_id(conn, dt.date(2026, 5, 4))  # Mon of W19
    assert rid == "2026-W19_gainesville__r2"


def test_third_run_of_week_appends_r3(tmp_path: Path) -> None:
    conn = connect(tmp_path / "t.duckdb")
    apply_schema(conn)
    _seed(conn, "2026-W19_gainesville")
    _seed(conn, "2026-W19_gainesville__r2")
    rid = next_run_id(conn, dt.date(2026, 5, 4))  # Mon of W19
    assert rid == "2026-W19_gainesville__r3"
