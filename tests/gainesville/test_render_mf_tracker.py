from __future__ import annotations

import datetime as dt
from pathlib import Path

from etl.gainesville.db import apply_schema, connect
from etl.gainesville.render_mf_tracker import render


def test_render_produces_markdown_with_required_sections(tmp_path: Path) -> None:
    conn = connect(tmp_path / "g.duckdb")
    apply_schema(conn)
    # Seed minimal fixture data.
    conn.execute(
        "INSERT INTO property_register VALUES "
        "('p1', 'P1', 'Tower View', '500 Pine Drive', 'Gainesville', "
        "'76240', TRUE, NULL, NULL, 'mf-large', 80, 1990, "
        "'ABC LLC', 'abc', ?, ?, 'cooke_cad', NULL)",
        [dt.datetime.now(dt.UTC), dt.datetime.now(dt.UTC)],
    )
    today = dt.date.today()
    conn.execute(
        "INSERT INTO canonical_listings VALUES "
        "('c1', 'p1', 'mf', '500 pine drive', 2.0, 1.0, 800, ?, ?, 0, 'active', "
        "1450, 1450, 1450, ['apartments_com'], ['o1'], 1.0, FALSE)",
        [today, today],
    )
    conn.execute(
        "INSERT INTO run_log VALUES (?, ?, ?, 'success', ['apartments_com'], "
        "[], 1, 1, 0, '')",
        ["r1", dt.datetime.now(dt.UTC), dt.datetime.now(dt.UTC)],
    )
    md = render(conn, month=dt.date(2026, 5, 1), for_edc=False)
    assert "# Gainesville, TX (Cooke County) — Multifamily Tracker" in md
    assert "## Universe" in md
    assert "## Active listings" in md
    assert "## Asking rents" in md
    assert "## Coverage diagnostics" in md
    assert "Tower View" in md
    assert "$1,450" in md or "1450" in md


def test_render_no_sqft_does_not_crash(tmp_path: Path) -> None:
    """Canonical with rent but NULL sqft must not raise TypeError on $/SF formatting."""
    conn = connect(tmp_path / "g.duckdb")
    apply_schema(conn)
    today = dt.date.today()
    # mf listing with rent but no sqft (sqft=NULL)
    conn.execute(
        "INSERT INTO canonical_listings VALUES "
        "('c1', NULL, 'mf', '200 elm street', 2.0, 1.0, NULL, ?, ?, 0, 'active', "
        "1300, 1300, 1300, ['craigslist'], ['o1'], 1.0, FALSE)",
        [today, today],
    )
    conn.execute(
        "INSERT INTO run_log VALUES (?, ?, ?, 'success', ['craigslist'], "
        "[], 1, 1, 0, '')",
        ["r1", dt.datetime.now(dt.UTC), dt.datetime.now(dt.UTC)],
    )
    md = render(conn, month=dt.date(2026, 5, 1), for_edc=False)
    assert "## Asking rents" in md
    # median_psf is None → em dash, not a crash
    assert "—" in md


def test_render_for_edc_redacts_specific_addresses(tmp_path: Path) -> None:
    conn = connect(tmp_path / "g.duckdb")
    apply_schema(conn)
    today = dt.date.today()
    conn.execute(
        "INSERT INTO canonical_listings VALUES "
        "('c1', NULL, 'sfr', '123 main street', 2.0, 1.0, 900, ?, ?, 0, 'active', "
        "1200, 1200, 1200, ['craigslist'], ['o1'], 1.0, FALSE)",
        [today, today],
    )
    md = render(conn, month=dt.date(2026, 5, 1), for_edc=True)
    assert "123 main street" not in md.lower()
    assert "## Universe" in md  # aggregates remain
