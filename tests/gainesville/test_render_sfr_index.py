from __future__ import annotations

import datetime as dt
from pathlib import Path

from etl.gainesville.db import apply_schema, connect
from etl.gainesville.render_sfr_index import render


def test_render_produces_required_sections(tmp_path: Path) -> None:
    conn = connect(tmp_path / "g.duckdb")
    apply_schema(conn)
    today = dt.date.today()
    # ZORI fixture — two years of data so YoY is computable.
    for i, month in enumerate([
        dt.date(2025, 4, 30), dt.date(2025, 5, 31),
        dt.date(2026, 4, 30), dt.date(2026, 5, 31),
    ]):
        conn.execute(
            "INSERT INTO zori_history VALUES (?, ?, ?, 'all', ?)",
            ["76240", month, 1400.0 + i * 10, dt.datetime.now(dt.UTC)],
        )
    # SFR canonicals — address is passed as a parameter to avoid '?' in string literals.
    for i, rent in enumerate([1300, 1400, 1500]):
        conn.execute(
            "INSERT INTO canonical_listings VALUES "
            "(?, NULL, 'sfr', ?, 3.0, 2.0, 1450, ?, ?, 0, 'active', "
            "?, ?, ?, ['craigslist'], ['o1'], 1.0, FALSE)",
            [f"c{i}", f"{i} main st", today, today, rent, rent, rent],
        )
    md = render(conn, month=dt.date(2026, 5, 1), zip_code="76240", for_edc=False)
    assert "# Gainesville, TX — SFR Rent Index" in md
    assert "## ZORI benchmark" in md
    assert "## Local median asking rent" in md
    assert "## ZORI vs. local divergence" in md
    assert "## Coverage diagnostics" in md
