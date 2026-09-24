from __future__ import annotations

import csv
import datetime as dt
from pathlib import Path

from etl.gainesville.db import apply_schema, connect
from etl.gainesville.dedup import export_review_queue


def test_export_review_queue_writes_csv(tmp_path: Path) -> None:
    conn = connect(tmp_path / "g.duckdb")
    apply_schema(conn)
    conn.execute(
        "INSERT INTO dedup_review VALUES "
        "(?, 'obsA', NULL, 'canonX', 0.78, 'tier=typo_tolerant', FALSE, NULL, NULL)",
        ["rev1"],
    )
    conn.execute(
        "INSERT INTO dedup_review VALUES "
        "(?, 'obsB', NULL, 'canonY', 0.70, 'tier=coord', FALSE, NULL, NULL)",
        ["rev2"],
    )
    out_path = tmp_path / "dedup_review_run1.csv"
    n = export_review_queue(conn, run_id="run1", out_path=out_path)
    assert n == 2
    with out_path.open() as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 2
    assert {r["review_id"] for r in rows} == {"rev1", "rev2"}
    assert "decision" in rows[0]


def test_export_review_queue_skips_decided(tmp_path: Path) -> None:
    conn = connect(tmp_path / "g.duckdb")
    apply_schema(conn)
    conn.execute(
        "INSERT INTO dedup_review VALUES "
        "(?, 'obsA', NULL, 'canonX', 0.78, 'tier=typo', TRUE, 'merge', ?)",
        ["rev1", dt.datetime.now(dt.UTC)],
    )
    out_path = tmp_path / "dedup_review_run1.csv"
    n = export_review_queue(conn, run_id="run1", out_path=out_path)
    assert n == 0
