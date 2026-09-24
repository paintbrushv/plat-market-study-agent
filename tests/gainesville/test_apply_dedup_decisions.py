from __future__ import annotations

import csv
from pathlib import Path

import duckdb
from etl.gainesville.apply_dedup_decisions import apply_decisions
from etl.gainesville.db import apply_schema, connect


def _seed_review(
    conn: duckdb.DuckDBPyConnection,
    review_id: str,
    obs_id: str,
    proposed_canonical: str,
) -> None:
    conn.execute(
        "INSERT INTO dedup_review VALUES (?, ?, NULL, ?, 0.78, 'tier=typo', FALSE, NULL, NULL)",
        [review_id, obs_id, proposed_canonical],
    )
    conn.execute(
        "INSERT INTO canonical_listings VALUES "
        "(?, NULL, 'sfr', '999 fake street', 2.0, 1.0, NULL, "
        "DATE '2026-05-01', DATE '2026-05-01', 0, 'active', 1200, 1200, 1200, "
        "['craigslist'], [?], 1.0, FALSE) ON CONFLICT DO NOTHING",
        [proposed_canonical, "obs-old"],
    )


def _write_decisions(path: Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=[
            "review_id", "candidate_a_obs_id", "proposed_canonical",
            "confidence", "reason", "decision",
        ])
        w.writeheader()
        for row in rows:
            w.writerow(row)


def test_decision_merge_sets_manual_override(tmp_path: Path) -> None:
    conn = connect(tmp_path / "g.duckdb")
    apply_schema(conn)
    _seed_review(conn, "rev1", "obsA", "canonX")
    csv_path = tmp_path / "decisions.csv"
    _write_decisions(csv_path, [{
        "review_id": "rev1",
        "candidate_a_obs_id": "obsA",
        "proposed_canonical": "canonX",
        "confidence": "0.78",
        "reason": "tier=typo",
        "decision": "merge",
    }])
    n = apply_decisions(csv_path, conn)
    assert n == 1
    row = conn.execute(
        "SELECT decided, decision, manual_override "
        "FROM dedup_review JOIN canonical_listings "
        "ON canonical_listings.canonical_id = dedup_review.proposed_canonical "
        "WHERE review_id = 'rev1'"
    ).fetchone()
    assert row[0] is True
    assert row[1] == "merge"
    assert row[2] is True


def test_decision_keep_separate_creates_new_canonical(tmp_path: Path) -> None:
    conn = connect(tmp_path / "g.duckdb")
    apply_schema(conn)
    _seed_review(conn, "rev1", "obsA", "canonX")
    # Insert the actual observation referenced.
    import datetime as dt
    conn.execute(
        "INSERT INTO raw_observations VALUES "
        "(?, 'r1', 'zillow', 'z1', NULL, ?, 'sfr', '111 other street', "
        "'111 other street', 1, NULL, '76240', NULL, NULL, 2.0, 1.0, NULL, "
        "1300, NULL, NULL, NULL, NULL, NULL, NULL, FALSE)",
        ["obsA", dt.datetime.now(dt.UTC)],
    )
    csv_path = tmp_path / "decisions.csv"
    _write_decisions(csv_path, [{
        "review_id": "rev1",
        "candidate_a_obs_id": "obsA",
        "proposed_canonical": "canonX",
        "confidence": "0.78",
        "reason": "tier=typo",
        "decision": "keep_separate",
    }])
    apply_decisions(csv_path, conn)
    n = conn.execute("SELECT COUNT(*) FROM canonical_listings").fetchone()[0]
    assert n == 2  # original canonX + new from obsA
