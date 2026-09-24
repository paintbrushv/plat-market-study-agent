from __future__ import annotations

import datetime as dt
from pathlib import Path

from etl.gainesville.db import apply_schema, connect
from etl.gainesville.dedup import attach_property_links, mark_stale


def test_active_listing_stays_active(tmp_path: Path) -> None:
    conn = connect(tmp_path / "g.duckdb")
    apply_schema(conn)
    today = dt.date.today()
    conn.execute(
        "INSERT INTO canonical_listings VALUES "
        "('c1', NULL, 'sfr', '123 main', 2.0, 1.0, NULL, ?, ?, 0, 'active', "
        "1200, 1200, 1200, ['cl'], ['o1'], 1.0, FALSE)",
        [today - dt.timedelta(days=14), today - dt.timedelta(days=2)],
    )
    mark_stale(conn, today=today)
    status = conn.execute("SELECT status FROM canonical_listings").fetchone()[0]
    assert status == "active"


def test_30day_stale_flips_to_inactive_30d(tmp_path: Path) -> None:
    conn = connect(tmp_path / "g.duckdb")
    apply_schema(conn)
    today = dt.date.today()
    conn.execute(
        "INSERT INTO canonical_listings VALUES "
        "('c1', NULL, 'sfr', '123 main', 2.0, 1.0, NULL, ?, ?, 0, 'active', "
        "1200, 1200, 1200, ['cl'], ['o1'], 1.0, FALSE)",
        [today - dt.timedelta(days=60), today - dt.timedelta(days=35)],
    )
    mark_stale(conn, today=today)
    status = conn.execute("SELECT status FROM canonical_listings").fetchone()[0]
    assert status == "inactive_30d"


def test_60day_stale_flips_to_rented_or_pulled(tmp_path: Path) -> None:
    conn = connect(tmp_path / "g.duckdb")
    apply_schema(conn)
    today = dt.date.today()
    conn.execute(
        "INSERT INTO canonical_listings VALUES "
        "('c1', NULL, 'sfr', '123 main', 2.0, 1.0, NULL, ?, ?, 0, 'inactive_30d', "
        "1200, 1200, 1200, ['cl'], ['o1'], 1.0, FALSE)",
        [today - dt.timedelta(days=120), today - dt.timedelta(days=70)],
    )
    mark_stale(conn, today=today)
    status = conn.execute("SELECT status FROM canonical_listings").fetchone()[0]
    assert status == "rented_or_pulled"


def test_attach_property_links_matches_by_address(tmp_path: Path) -> None:
    conn = connect(tmp_path / "g.duckdb")
    apply_schema(conn)
    conn.execute(
        "INSERT INTO property_register VALUES "
        "('p1', 'P-001', 'Tower View Apts', '500 Pine Drive', 'Gainesville', "
        "'76240', TRUE, NULL, NULL, 'mf-large', 80, 1992, NULL, NULL, NULL, NULL, "
        "'cooke_cad', NULL)"
    )
    conn.execute(
        "INSERT INTO canonical_listings VALUES "
        "('c1', NULL, 'mf', '500 pine drive', 2.0, 1.0, NULL, "
        "DATE '2026-05-01', DATE '2026-05-09', 8, 'active', 1400, 1400, 1400, "
        "['apartments_com'], ['o1'], 1.0, FALSE)"
    )
    n = attach_property_links(conn)
    assert n == 1
    pid = conn.execute(
        "SELECT property_id FROM canonical_listings WHERE canonical_id='c1'"
    ).fetchone()[0]
    assert pid == "p1"


def test_attach_links_by_coord_when_address_doesnt_match(tmp_path: Path) -> None:
    """Canonical with no address but lat/lon close to a property_register entry gets linked."""
    conn = connect(tmp_path / "g.duckdb")
    apply_schema(conn)
    # Property at (33.626, -97.133)
    conn.execute(
        "INSERT INTO property_register VALUES "
        "('p1', 'P-001', 'Coord Test Apts', '500 Pine Drive', 'Gainesville', "
        "'76240', TRUE, 33.626, -97.133, 'mf-small', 12, 2000, NULL, NULL, NULL, NULL, "
        "'cooke_cad', NULL)"
    )
    # Observation at (33.6262, -97.1331) — within 0.0005 deg tolerance
    conn.execute(
        "INSERT INTO raw_observations VALUES "
        "('o1', 'run1', 'craigslist', 'sid1', 'http://x', "
        "TIMESTAMPTZ '2026-05-09 10:00:00+00', 'sfr', '', NULL, 1, "
        "'Gainesville', '76240', 33.6262, -97.1331, 3.0, 2.0, NULL, 2307, "
        "NULL, NULL, NULL, 'Test post', NULL, NULL, FALSE)"
    )
    # Canonical with no address_normalized, linked to that observation
    conn.execute(
        "INSERT INTO canonical_listings VALUES "
        "('c1', NULL, 'sfr', NULL, 3.0, 2.0, NULL, "
        "DATE '2026-05-09', DATE '2026-05-09', 0, 'active', 2307, 2307, 2307, "
        "['craigslist'], ['o1'], 1.0, FALSE)"
    )
    n = attach_property_links(conn)
    assert n == 1
    pid = conn.execute(
        "SELECT property_id FROM canonical_listings WHERE canonical_id='c1'"
    ).fetchone()[0]
    assert pid == "p1"


def test_attach_links_skips_when_no_coords(tmp_path: Path) -> None:
    """Canonical with no address and no lat/lon in its observations stays unlinked."""
    conn = connect(tmp_path / "g.duckdb")
    apply_schema(conn)
    conn.execute(
        "INSERT INTO property_register VALUES "
        "('p1', 'P-001', 'Coord Test Apts', '500 Pine Drive', 'Gainesville', "
        "'76240', TRUE, 33.626, -97.133, 'mf-small', 12, 2000, NULL, NULL, NULL, NULL, "
        "'cooke_cad', NULL)"
    )
    # Observation with NULL lat/lon
    conn.execute(
        "INSERT INTO raw_observations VALUES "
        "('o1', 'run1', 'craigslist', 'sid1', 'http://x', "
        "TIMESTAMPTZ '2026-05-09 10:00:00+00', 'sfr', '', NULL, 1, "
        "'Gainesville', '76240', NULL, NULL, 3.0, 2.0, NULL, 2800, "
        "NULL, NULL, NULL, 'Lake Kiowa house', NULL, NULL, FALSE)"
    )
    conn.execute(
        "INSERT INTO canonical_listings VALUES "
        "('c1', NULL, 'sfr', NULL, 3.0, 2.0, NULL, "
        "DATE '2026-05-09', DATE '2026-05-09', 0, 'active', 2800, 2800, 2800, "
        "['craigslist'], ['o1'], 1.0, FALSE)"
    )
    n = attach_property_links(conn)
    assert n == 0
    pid = conn.execute(
        "SELECT property_id FROM canonical_listings WHERE canonical_id='c1'"
    ).fetchone()[0]
    assert pid is None


def test_attach_links_address_takes_priority_over_coord(tmp_path: Path) -> None:
    """When address match exists, it wins over coord proximity to a different property."""
    conn = connect(tmp_path / "g.duckdb")
    apply_schema(conn)
    # p_addr: matches by normalized address "500 pine drive"
    conn.execute(
        "INSERT INTO property_register VALUES "
        "('p_addr', 'P-001', 'Address Prop', '500 Pine Drive', 'Gainesville', "
        "'76240', TRUE, 99.0, 99.0, 'mf-small', 12, 2000, NULL, NULL, NULL, NULL, "
        "'cooke_cad', NULL)"
    )
    # p_coord: nearby coords but different address
    conn.execute(
        "INSERT INTO property_register VALUES "
        "('p_coord', 'P-002', 'Coord Prop', '9999 Far Away Blvd', 'Gainesville', "
        "'76240', TRUE, 33.626, -97.133, 'mf-small', 8, 2005, NULL, NULL, NULL, NULL, "
        "'cooke_cad', NULL)"
    )
    # Observation: coords near p_coord, but address normalizes to p_addr
    conn.execute(
        "INSERT INTO raw_observations VALUES "
        "('o1', 'run1', 'apartments_com', 'sid1', 'http://x', "
        "TIMESTAMPTZ '2026-05-09 10:00:00+00', 'mf', '500 Pine Drive', "
        "'500 pine drive', 1, 'Gainesville', '76240', 33.6262, -97.1331, 2.0, 1.0, "
        "NULL, 1400, NULL, NULL, NULL, 'Nice place', NULL, NULL, FALSE)"
    )
    conn.execute(
        "INSERT INTO canonical_listings VALUES "
        "('c1', NULL, 'mf', '500 pine drive', 2.0, 1.0, NULL, "
        "DATE '2026-05-09', DATE '2026-05-09', 0, 'active', 1400, 1400, 1400, "
        "['apartments_com'], ['o1'], 1.0, FALSE)"
    )
    n = attach_property_links(conn)
    assert n == 1
    pid = conn.execute(
        "SELECT property_id FROM canonical_listings WHERE canonical_id='c1'"
    ).fetchone()[0]
    # Address match wins — not the coord-proximity property
    assert pid == "p_addr"
