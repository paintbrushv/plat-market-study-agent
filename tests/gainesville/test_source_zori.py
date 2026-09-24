from __future__ import annotations

from pathlib import Path

from etl.gainesville.dataclasses import Catchment, CollectionStatus
from etl.gainesville.db import apply_schema, connect
from etl.gainesville.sources.zori import collect_from_csv

FIXTURE = Path(__file__).parent / "fixtures" / "zori_sample.csv"
FIXTURE_REALISTIC = Path(__file__).parent / "fixtures" / "zori_realistic.csv"


def test_zori_loads_filtered_zips(tmp_path: Path) -> None:
    db = tmp_path / "g.duckdb"
    conn = connect(db)
    apply_schema(conn)
    catchment = Catchment(
        county_fips="48097",
        zip_codes=("76240", "76252"),
        bbox=(33.5, -97.4, 33.8, -96.8),
        city_limits_geojson_path=None,
    )
    result = collect_from_csv(FIXTURE, catchment, conn, home_type="all")
    assert result.status == CollectionStatus.OK
    rows = conn.execute(
        "SELECT zip, month, zori_value FROM zori_history ORDER BY zip, month"
    ).fetchall()
    assert len(rows) == 8  # 2 zips x 4 months
    zips = {r[0] for r in rows}
    assert zips == {"76240", "76252"}
    # 76240 jan = 1450
    val = conn.execute(
        "SELECT zori_value FROM zori_history WHERE zip='76240' AND month='2024-01-31'"
    ).fetchone()[0]
    assert val == 1450.0


def test_zori_upsert_is_idempotent(tmp_path: Path) -> None:
    db = tmp_path / "g.duckdb"
    conn = connect(db)
    apply_schema(conn)
    catchment = Catchment("48097", ("76240",), (0, 0, 0, 0), None)
    collect_from_csv(FIXTURE, catchment, conn, home_type="all")
    collect_from_csv(FIXTURE, catchment, conn, home_type="all")  # second run
    n = conn.execute("SELECT COUNT(*) FROM zori_history").fetchone()[0]
    assert n == 4  # one zip x 4 months, no duplicates


def test_zori_realistic_csv_with_extra_metadata_columns(tmp_path: Path) -> None:
    """Real Zillow CSVs include State, City, Metro, CountyName metadata columns.

    The unpivot must restrict to date-shaped column names (YYYY-MM-DD) so those
    text columns are not treated as month values, which would cause a CAST failure.
    """
    db = tmp_path / "g.duckdb"
    conn = connect(db)
    apply_schema(conn)
    catchment = Catchment(
        county_fips="48097",
        zip_codes=("76240", "76252"),
        bbox=(33.5, -97.4, 33.8, -96.8),
        city_limits_geojson_path=None,
    )
    result = collect_from_csv(FIXTURE_REALISTIC, catchment, conn, home_type="all")
    assert result.status == CollectionStatus.OK, (
        f"Expected OK, got {result.status}: {result.diagnostics}"
    )
    rows = conn.execute(
        "SELECT zip, month, zori_value FROM zori_history ORDER BY zip, month"
    ).fetchall()
    assert len(rows) == 8, f"Expected 8 rows (2 zips x 4 months), got {len(rows)}"
    zips = {r[0] for r in rows}
    assert zips == {"76240", "76252"}
    # Spot-check that State/City/Metro/CountyName were NOT treated as month values.
    months = {str(r[1]) for r in rows}
    for m in months:
        assert m.startswith("2024-"), f"Unexpected month value leaked from metadata: {m!r}"
