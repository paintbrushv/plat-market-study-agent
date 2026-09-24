"""One-shot rewrite of address_normalized for observations with stale
addr_norm_version. Run after bumping ADDR_NORM_VERSION.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import duckdb

from etl.gainesville.address_normalize import ADDR_NORM_VERSION, normalize_address
from etl.gainesville.db import connect


def renormalize(conn: duckdb.DuckDBPyConnection) -> int:
    rows = conn.execute(
        "SELECT observation_id, address_raw FROM raw_observations "
        "WHERE addr_norm_version < ?",
        [ADDR_NORM_VERSION],
    ).fetchall()
    n = 0
    for obs_id, addr_raw in rows:
        new_norm = normalize_address(addr_raw)
        conn.execute(
            "UPDATE raw_observations "
            "SET address_normalized = ?, addr_norm_version = ? "
            "WHERE observation_id = ?",
            [new_norm, ADDR_NORM_VERSION, obs_id],
        )
        n += 1
    return n


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Re-normalize historical observation addresses.")
    parser.add_argument("--db", type=Path, default=Path("data/local/gainesville.duckdb"))
    args = parser.parse_args(argv)
    conn = connect(args.db)
    n = renormalize(conn)
    print(f"renormalized {n} rows to addr_norm_version={ADDR_NORM_VERSION}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
