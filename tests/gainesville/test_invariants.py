from __future__ import annotations

import datetime as dt
from pathlib import Path

import duckdb
import pytest
from etl.gainesville.db import apply_schema, connect


def _insert_obs(conn: duckdb.DuckDBPyConnection, obs_id: str, run_id: str) -> None:
    conn.execute(
        "INSERT INTO raw_observations (observation_id, run_id, source, scraped_at) "
        "VALUES (?, ?, 'test', ?)",
        [obs_id, run_id, dt.datetime.now(dt.UTC)],
    )


def test_raw_observations_count_is_monotonic(tmp_path: Path) -> None:
    """raw_observations is append-only: counts only ever grow within a session.

    This is a tripwire — if a future PR introduces an UPDATE/DELETE on
    raw_observations, this test fails before it lands.
    """
    conn = connect(tmp_path / "t.duckdb")
    apply_schema(conn)
    counts: list[int] = []
    for i in range(5):
        _insert_obs(conn, f"obs-{i}", "run-1")
        counts.append(conn.execute("SELECT COUNT(*) FROM raw_observations").fetchone()[0])
    assert counts == sorted(counts)
    assert counts == [1, 2, 3, 4, 5]


def test_raw_observations_rejects_duplicate_id(tmp_path: Path) -> None:
    conn = connect(tmp_path / "t.duckdb")
    apply_schema(conn)
    _insert_obs(conn, "obs-A", "run-1")
    with pytest.raises(duckdb.Error):
        _insert_obs(conn, "obs-A", "run-1")
