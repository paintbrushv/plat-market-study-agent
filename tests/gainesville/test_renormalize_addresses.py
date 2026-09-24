from __future__ import annotations

import datetime as dt
from pathlib import Path

from etl.gainesville.db import apply_schema, connect
from etl.gainesville.renormalize_addresses import renormalize


def test_renormalize_rewrites_old_version_rows(tmp_path: Path) -> None:
    conn = connect(tmp_path / "g.duckdb")
    apply_schema(conn)
    conn.execute(
        "INSERT INTO raw_observations VALUES "
        "('o1', 'r1', 'craigslist', 's1', NULL, ?, 'sfr', "
        "'123 Main St., Gainesville, TX 76240', 'old normalized form', 0, "
        "NULL, NULL, NULL, NULL, NULL, NULL, NULL, 1200, NULL, NULL, NULL, "
        "NULL, NULL, NULL, FALSE)",
        [dt.datetime.now(dt.UTC)],
    )
    n = renormalize(conn)
    assert n == 1
    row = conn.execute(
        "SELECT address_normalized, addr_norm_version FROM raw_observations"
    ).fetchone()
    assert row[0] == "123 main street"
    assert row[1] == 1


def test_renormalize_skips_current_version_rows(tmp_path: Path) -> None:
    conn = connect(tmp_path / "g.duckdb")
    apply_schema(conn)
    conn.execute(
        "INSERT INTO raw_observations VALUES "
        "('o1', 'r1', 'craigslist', 's1', NULL, ?, 'sfr', "
        "'123 Main St', '123 main street', 1, "
        "NULL, NULL, NULL, NULL, NULL, NULL, NULL, 1200, NULL, NULL, NULL, "
        "NULL, NULL, NULL, FALSE)",
        [dt.datetime.now(dt.UTC)],
    )
    n = renormalize(conn)
    assert n == 0
