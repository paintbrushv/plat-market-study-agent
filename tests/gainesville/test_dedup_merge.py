from __future__ import annotations

import datetime as dt
from pathlib import Path

import duckdb
from etl.gainesville.db import apply_schema, connect
from etl.gainesville.dedup import run_dedup


def _seed_obs(
    conn: duckdb.DuckDBPyConnection,
    obs_id: str,
    run_id: str,
    addr: str,
    beds: float,
    baths: float,
    rent: int,
    source: str = "craigslist",
    sid: str = "x",
) -> None:
    conn.execute(
        "INSERT INTO raw_observations VALUES "
        "(?, ?, ?, ?, NULL, ?, 'sfr', ?, ?, 1, NULL, NULL, NULL, NULL, ?, ?, NULL, ?, "
        "NULL, NULL, NULL, NULL, NULL, NULL, FALSE)",
        [obs_id, run_id, source, sid, dt.datetime.now(dt.UTC),
         addr, addr, beds, baths, rent],
    )


def _seed_obs_with_coords(
    conn: duckdb.DuckDBPyConnection,
    obs_id: str,
    run_id: str,
    addr: str | None,
    beds: float,
    baths: float,
    rent: int,
    lat: float,
    lon: float,
    source: str = "craigslist",
    sid: str = "x",
) -> None:
    conn.execute(
        "INSERT INTO raw_observations VALUES "
        "(?, ?, ?, ?, NULL, ?, 'sfr', ?, ?, 1, NULL, NULL, ?, ?, ?, ?, NULL, ?, "
        "NULL, NULL, NULL, NULL, NULL, NULL, FALSE)",
        [obs_id, run_id, source, sid, dt.datetime.now(dt.UTC),
         addr, addr, lat, lon, beds, baths, rent],
    )


def test_first_run_creates_new_canonicals(tmp_path: Path) -> None:
    conn = connect(tmp_path / "g.duckdb")
    apply_schema(conn)
    _seed_obs(conn, "o1", "r1", "123 main street", 2.0, 1.0, 1200)
    _seed_obs(conn, "o2", "r1", "456 oak avenue", 3.0, 2.0, 1500)
    summary = run_dedup(conn, run_id="r1")
    assert summary.new_canonicals == 2
    assert summary.merged == 0
    assert summary.review_queued == 0
    n = conn.execute("SELECT COUNT(*) FROM canonical_listings").fetchone()[0]
    assert n == 2


def test_second_run_merges_strict_match(tmp_path: Path) -> None:
    conn = connect(tmp_path / "g.duckdb")
    apply_schema(conn)
    _seed_obs(conn, "o1", "r1", "123 main street", 2.0, 1.0, 1200)
    run_dedup(conn, run_id="r1")
    _seed_obs(conn, "o2", "r2", "123 main street", 2.0, 1.0, 1250, source="zillow", sid="z1")
    summary = run_dedup(conn, run_id="r2")
    assert summary.merged == 1
    assert summary.new_canonicals == 0
    rows = conn.execute(
        "SELECT current_rent, sources_seen FROM canonical_listings"
    ).fetchall()
    assert len(rows) == 1
    rent, sources = rows[0]
    assert rent == 1250
    assert "craigslist" in sources and "zillow" in sources


def test_canonicals_carry_sqft_from_observation(tmp_path: Path) -> None:
    conn = connect(tmp_path / "g.duckdb")
    apply_schema(conn)
    conn.execute(
        "INSERT INTO raw_observations VALUES "
        "(?, 'r1', 'apartments_com', 's1', NULL, ?, 'mf', '500 pine drive', "
        "'500 pine drive', 1, NULL, '76240', NULL, NULL, 2.0, 1.0, 950, 1450, "
        "NULL, NULL, NULL, NULL, NULL, NULL, FALSE)",
        ["o1", dt.datetime.now(dt.UTC)],
    )
    run_dedup(conn, run_id="r1")
    sqft = conn.execute("SELECT sqft FROM canonical_listings").fetchone()[0]
    assert sqft == 950


def test_merge_fills_sqft_when_canonical_has_none(tmp_path: Path) -> None:
    """A second observation with sqft fills the canonical's NULL sqft via merge."""
    conn = connect(tmp_path / "g.duckdb")
    apply_schema(conn)
    # First obs: no sqft
    conn.execute(
        "INSERT INTO raw_observations VALUES "
        "(?, 'r1', 'craigslist', 's1', NULL, ?, 'mf', '123 main street', "
        "'123 main street', 1, NULL, '76240', NULL, NULL, 2.0, 1.0, NULL, 1200, "
        "NULL, NULL, NULL, NULL, NULL, NULL, FALSE)",
        ["o1", dt.datetime.now(dt.UTC)],
    )
    run_dedup(conn, run_id="r1")
    sqft_before = conn.execute("SELECT sqft FROM canonical_listings").fetchone()[0]
    assert sqft_before is None
    # Second obs: same address/beds/baths, but has sqft
    conn.execute(
        "INSERT INTO raw_observations VALUES "
        "(?, 'r2', 'zillow', 's2', NULL, ?, 'mf', '123 main street', "
        "'123 main street', 1, NULL, '76240', NULL, NULL, 2.0, 1.0, 875, 1200, "
        "NULL, NULL, NULL, NULL, NULL, NULL, FALSE)",
        ["o2", dt.datetime.now(dt.UTC)],
    )
    run_dedup(conn, run_id="r2")
    sqft_after = conn.execute("SELECT sqft FROM canonical_listings").fetchone()[0]
    assert sqft_after == 875


def test_low_confidence_match_goes_to_review(tmp_path: Path) -> None:
    """Coord-tier match (0.70) routes to review when score < AUTO_MERGE_THRESHOLD.

    The canonical is seeded from an observation that *carries coordinates* so
    that list_contains(cl.observation_ids, o.observation_id) can match the
    coords against the canonical's own observations — not a ghost observation.
    """
    conn = connect(tmp_path / "g.duckdb")
    apply_schema(conn)
    # o1 has coords 33.6261, -97.1331 and an address.  After dedup it becomes
    # a canonical; the canonical's observation_ids = ['o1'].
    _seed_obs_with_coords(
        conn, "o1", "r1", "123 main street", 2.0, 1.0, 1200, 33.6261, -97.1331
    )
    run_dedup(conn, run_id="r1")
    # Remove the address on the canonical so strict/bath-flex tiers can't fire;
    # only the coord tier can match o2 to this canonical.
    conn.execute("UPDATE canonical_listings SET address_normalized = NULL")
    # o2: same coords, same beds, no address — coord-only match (score 0.70).
    conn.execute(
        "INSERT INTO raw_observations VALUES "
        "(?, ?, 'zillow', 'z2', NULL, ?, 'sfr', NULL, NULL, 1, NULL, NULL, "
        "33.6261, -97.1331, 2.0, NULL, NULL, 1300, NULL, NULL, NULL, NULL, NULL, NULL, FALSE)",
        ["o2", "r2", dt.datetime.now(dt.UTC)],
    )
    summary = run_dedup(conn, run_id="r2")
    assert summary.review_queued >= 1
