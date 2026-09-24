"""Apply manual dedup decisions from an operator-edited CSV.

Decision values:
  - 'merge'           : merge the obs into the proposed canonical, set manual_override.
  - 'keep_separate'   : create a new canonical from the obs.
  - 'merge_into:<id>' : merge into a different canonical_id.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import logging
import sys
import uuid
from pathlib import Path

import duckdb

from etl.gainesville.db import connect

logger = logging.getLogger(__name__)


def apply_decisions(csv_path: Path, conn: duckdb.DuckDBPyConnection) -> int:
    n = 0
    with csv_path.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            decision = (row.get("decision") or "").strip()
            if not decision:
                continue
            review_id = row["review_id"]
            obs_id = row["candidate_a_obs_id"]
            proposed = row["proposed_canonical"]
            if decision == "merge":
                _merge(conn, obs_id, proposed)
            elif decision == "keep_separate":
                _create_new_from_obs(conn, obs_id)
            elif decision.startswith("merge_into:"):
                target = decision.split(":", 1)[1].strip()
                _merge(conn, obs_id, target)
            else:
                logger.warning("unknown decision %r for review %s", decision, review_id)
                continue
            conn.execute(
                "UPDATE dedup_review SET decided=TRUE, decision=?, decided_at=? "
                "WHERE review_id=?",
                [decision, dt.datetime.now(dt.UTC), review_id],
            )
            n += 1
    return n


def _merge(conn: duckdb.DuckDBPyConnection, obs_id: str, canonical_id: str) -> None:
    obs = conn.execute(
        "SELECT source, asking_rent FROM raw_observations WHERE observation_id=?",
        [obs_id],
    ).fetchone()
    source, rent = (obs[0], obs[1]) if obs else ("manual", None)
    conn.execute(
        "UPDATE canonical_listings SET "
        "current_rent = COALESCE(?, current_rent), "
        "sources_seen = list_distinct(list_concat(sources_seen, [?])), "
        "observation_ids = list_concat(observation_ids, [?]), "
        "manual_override = TRUE "
        "WHERE canonical_id = ?",
        [rent, source, obs_id, canonical_id],
    )


def _create_new_from_obs(conn: duckdb.DuckDBPyConnection, obs_id: str) -> str:
    o = conn.execute(
        "SELECT listing_kind, address_normalized, beds, baths, asking_rent, source "
        "FROM raw_observations WHERE observation_id = ?",
        [obs_id],
    ).fetchone()
    if not o:
        raise ValueError(f"observation {obs_id} not found")
    kind, addr, beds, baths, rent, source = o
    canonical_id = "c_" + uuid.uuid4().hex[:16]
    today = dt.date.today()
    conn.execute(
        "INSERT INTO canonical_listings VALUES "
        "(?, NULL, ?, ?, ?, ?, NULL, ?, ?, 0, 'active', ?, ?, ?, [?], [?], 1.0, TRUE)",
        [canonical_id, kind, addr, beds, baths, today, today,
         rent, rent, rent, source, obs_id],
    )
    return canonical_id


def unmark_override(conn: duckdb.DuckDBPyConnection, canonical_id: str) -> None:
    conn.execute(
        "UPDATE canonical_listings SET manual_override = FALSE WHERE canonical_id = ?",
        [canonical_id],
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Apply dedup decisions from CSV.")
    parser.add_argument("csv", type=Path, nargs="?", help="Path to decisions CSV")
    parser.add_argument(
        "--db", type=Path, default=Path("data/local/gainesville.duckdb"),
    )
    parser.add_argument("--unmark", help="Clear manual_override on a canonical_id")
    args = parser.parse_args(argv)
    conn = connect(args.db)
    if args.unmark:
        unmark_override(conn, args.unmark)
        print(f"unmarked override on {args.unmark}")
        return 0
    if args.csv is None:
        parser.error("csv argument is required when --unmark is not specified")
    n = apply_decisions(args.csv, conn)
    print(f"applied {n} decisions")
    return 0


if __name__ == "__main__":
    sys.exit(main())
