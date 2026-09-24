"""Tests for etl.gainesville.sources.cooke_cad_import.

The fixture uses sqlite3 to synthesize a klement.db look-alike with the
real schema (property table, no schema_version table).
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest
from etl.gainesville.db import apply_schema, connect
from etl.gainesville.geo import load_polygon
from etl.gainesville.sources.cooke_cad_import import (
    KlementSchemaMismatch,
    import_parcels,
)


def _make_city_limits_geojson(path: Path) -> Path:
    """Write a synthetic GeoJSON polygon that covers a small box around
    Gainesville TX (33.61 ≤ lat ≤ 33.65, -97.18 ≤ lon ≤ -97.10).

    Points inside: lat 33.626, lon -97.133  (P1 in _make_klement_db)
    Points outside: lat 33.50, lon -97.00   (synthetic row added in city-limits tests)
    """
    geojson = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {"NAME": "Gainesville (synthetic test polygon)"},
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [
                        [
                            [-97.18, 33.61],
                            [-97.10, 33.61],
                            [-97.10, 33.65],
                            [-97.18, 33.65],
                            [-97.18, 33.61],
                        ]
                    ],
                },
            }
        ],
    }
    path.write_text(json.dumps(geojson), encoding="utf-8")
    return path


def _make_klement_db(path: Path, *, omit_columns: list[str] | None = None) -> None:
    """Synthesize a klement.db look-alike with the real SQLite property table.

    Args:
        path: Destination path for the SQLite file.
        omit_columns: Column names to omit from the CREATE TABLE statement,
            used to exercise KlementSchemaMismatch.
    """
    all_columns = [
        "parcel_id TEXT",
        "county TEXT",
        "owner_name TEXT",
        "property_address TEXT",
        "city TEXT",
        "zip TEXT",
        "latitude REAL",
        "longitude REAL",
        "property_type TEXT",
        "unit_count INTEGER",
        "year_built INTEGER",
    ]
    if omit_columns:
        all_columns = [
            c for c in all_columns
            if c.split()[0] not in omit_columns
        ]
    cols_ddl = ", ".join(all_columns)

    k = sqlite3.connect(str(path))
    k.execute(f"CREATE TABLE property ({cols_ddl})")

    # Only insert rows when we have the full schema (otherwise omitted-column
    # tests just need the table to exist with missing cols).
    if not omit_columns:
        k.executemany(
            "INSERT INTO property VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                # Genuine residential_mf — should be included
                # lat=33.626, lon=-97.133 → INSIDE the test city-limits polygon
                ("P1", "cooke", "KARL KLEMENT LLC",
                 "100 Main St", "Gainesville", "76240",
                 33.626, -97.133, "residential_mf", 24, 1985),
                # Large MF — should be mf-large
                # lat=33.630, lon=-97.140 → INSIDE the test city-limits polygon
                ("P2", "cooke", "ABC HOLDINGS LP",
                 "200 Oak Ave", "Gainesville", "76240",
                 33.630, -97.140, "residential_mf", 80, 2008),
                # SFR — should be filtered out
                ("P3", "cooke", "Jane Doe",
                 "300 Elm St", "Gainesville", "76240",
                 33.620, -97.120, "residential_sf", None, 1972),
                # property_type='other' but owner_name contains APARTMENTS — catch it
                # lat=33.615, lon=-97.150 → INSIDE the test polygon
                ("P4", "cooke", "SUNSET APARTMENTS LLC",
                 "400 Pine Dr", "Gainesville", "76240",
                 33.615, -97.150, "other", 12, 1995),
                # property_type='other', owner_name contains APTS — catch it
                # lat=33.610, lon=-97.160 → INSIDE the test polygon
                ("P5", "cooke", "FIRST APTS INC",
                 "500 Cedar Ln", "Gainesville", "76240",
                 33.610, -97.160, "other", 6, 2001),
                # commercial — filtered out (owner doesn't match keywords)
                ("P6", "cooke", "COMMERCIAL CORP",
                 "600 Market St", "Gainesville", "76240",
                 33.640, -97.110, "commercial", None, 1990),
                # Different county — should be filtered out
                ("P7", "denton", "NORTH TX APTS",
                 "700 North St", "Denton", "76201",
                 33.215, -97.133, "residential_mf", 20, 2000),
                # CCAD-misclassified personal-property lease: NULL address — must be excluded
                ("P-FAKE", "cooke", "BMW FINANCIAL SERVICES NA LLC",
                 None, "Gainesville", "76240",
                 None, None, "residential_mf", None, None),
                # Personal-property lease with blank address — must be excluded
                ("P-FAKE2", "cooke", "HONDA LEASE TRUST",
                 "   ", "Gainesville", "76240",
                 None, None, "residential_mf", None, None),
                # residential_mf but owner contains FINANCIAL — excluded by name guard
                ("P-FIN", "cooke", "TEXAS FINANCIAL REALTY LLC",
                 "800 Finance Blvd", "Gainesville", "76240",
                 33.632, -97.135, "residential_mf", 10, 2005),
                # residential_mf OUTSIDE the test city-limits polygon
                # lat=33.50, lon=-97.00 → clearly outside [-97.18..-97.10, 33.61..33.65]
                ("P-OUT", "cooke", "RURAL APARTMENTS LLC",
                 "999 Rural Rd", "Gainesville", "76240",
                 33.50, -97.00, "residential_mf", 8, 2010),
                # SFR that matches owner_name keyword "APARTMENTS" — excluded by
                # parcel_id_exclusions when the operator confirms it's SFR.
                ("P-SFR-EXCL", "cooke", "RICOCHET APARTMENTS LLC",
                 "1115 Oxford Dr", "Gainesville", "76240",
                 33.618, -97.130, "other", 1, 2005),
            ],
        )
    k.commit()
    k.close()


def test_import_parcels_filters_to_mf(tmp_path: Path) -> None:
    """Should include residential_mf rows and APARTMENTS/APTS-named rows from Cooke County."""
    klement = tmp_path / "klement.db"
    _make_klement_db(klement)
    conn = connect(tmp_path / "g.duckdb")
    apply_schema(conn)

    result = import_parcels(
        klement_db_path=klement,
        target_conn=conn,
        county_fips="48097",
        property_types=["residential_mf"],
        owner_name_keywords=["APARTMENTS", "APTS"],
    )

    assert result.status.value == "ok"
    rows = conn.execute(
        "SELECT parcel_id, owner_entity_normalized, units FROM property_register ORDER BY parcel_id"
    ).fetchall()
    # P1, P2, P-OUT (residential_mf Cooke), P4 (APARTMENTS keyword), P5 (APTS keyword),
    # P-SFR-EXCL (APARTMENTS keyword, no exclusion list passed — it flows through).
    # P3 (residential_sf), P6 (commercial, no keyword), P7 (wrong county) excluded.
    parcel_ids = [r[0] for r in rows]
    assert set(parcel_ids) == {"P1", "P2", "P-OUT", "P4", "P5", "P-SFR-EXCL"}, (
        f"Expected P1/P2/P-OUT/P4/P5/P-SFR-EXCL; got {parcel_ids}"
    )
    # P2 has 80 units → mf-large
    p2_type = conn.execute(
        "SELECT property_type FROM property_register WHERE parcel_id='P2'"
    ).fetchone()
    assert p2_type is not None and p2_type[0] == "mf-large"
    # Diagnostics should report counts
    assert result.diagnostics["total"] == 6


def test_import_parcels_missing_db_returns_ok_with_warning(tmp_path: Path) -> None:
    """A missing klement.db must return OK with klement_unavailable diagnostic."""
    conn = connect(tmp_path / "g.duckdb")
    apply_schema(conn)
    result = import_parcels(
        klement_db_path=tmp_path / "does-not-exist.db",
        target_conn=conn,
        county_fips="48097",
    )
    assert result.status.value == "ok"
    assert "klement_unavailable" in result.diagnostics


def test_import_parcels_missing_columns_raises_schema_mismatch(tmp_path: Path) -> None:
    """Missing required columns must raise KlementSchemaMismatch (not silently pass)."""
    klement = tmp_path / "klement.db"
    # Create a property table that is missing 'unit_count' and 'latitude'
    _make_klement_db(klement, omit_columns=["unit_count", "latitude"])
    conn = connect(tmp_path / "g.duckdb")
    apply_schema(conn)
    with pytest.raises(KlementSchemaMismatch) as exc_info:
        import_parcels(
            klement_db_path=klement,
            target_conn=conn,
            county_fips="48097",
        )
    assert "unit_count" in str(exc_info.value) or "latitude" in str(exc_info.value)


def test_personal_property_leases_excluded(tmp_path: Path) -> None:
    """Personal-property-lease accounts (NULL/blank address, FINANCIAL/LEASE TRUST owners)
    must be excluded even when their property_type is 'residential_mf'."""
    klement = tmp_path / "klement.db"
    _make_klement_db(klement)
    conn = connect(tmp_path / "g.duckdb")
    apply_schema(conn)

    result = import_parcels(
        klement_db_path=klement,
        target_conn=conn,
        county_fips="48097",
        property_types=["residential_mf"],
        owner_name_keywords=["APARTMENTS", "APTS"],
    )

    assert result.status.value == "ok"
    parcel_ids = {
        r[0]
        for r in conn.execute("SELECT parcel_id FROM property_register").fetchall()
    }
    # NULL-address personal-property lease must not appear
    assert "P-FAKE" not in parcel_ids, "BMW Financial (NULL address) leaked into register"
    # Blank-address personal-property lease must not appear
    assert "P-FAKE2" not in parcel_ids, "Honda Lease Trust (blank address) leaked into register"
    # FINANCIAL owner_name guard must fire even on residential_mf type rows
    assert "P-FIN" not in parcel_ids, "FINANCIAL owner leaked into register"
    # Real MF properties must still be present
    assert {"P1", "P2", "P4", "P5", "P-OUT"}.issubset(parcel_ids), (
        f"Expected real MF parcels missing; got {parcel_ids}"
    )


def test_clean_slate_refresh_removes_stale_rows(tmp_path: Path) -> None:
    """Re-running import_parcels must remove previously-imported rows that no
    longer pass the filter (e.g. personal-property leases inserted before the
    WHERE-clause tightening)."""
    klement = tmp_path / "klement.db"
    _make_klement_db(klement)
    conn = connect(tmp_path / "g.duckdb")
    apply_schema(conn)

    # Manually plant a stale row that looks like a personal-property lease
    import hashlib, datetime as dt
    stale_id = "p_" + hashlib.sha1(b"P-STALE").hexdigest()[:12]
    conn.execute(
        "INSERT INTO property_register VALUES "
        "(?, 'P-STALE', NULL, '999 Fake Blvd', 'Gainesville', '76240', NULL, "
        "33.0, -97.0, 'mf-small', 5, 2000, 'BMW FINANCIAL SERVICES NA LLC', "
        "'BMW Financial', ?, ?, 'cooke_cad', NULL)",
        [stale_id, dt.datetime.now(dt.UTC), dt.datetime.now(dt.UTC)],
    )
    before = conn.execute("SELECT COUNT(*) FROM property_register").fetchone()[0]
    assert before == 1

    import_parcels(
        klement_db_path=klement,
        target_conn=conn,
        county_fips="48097",
        property_types=["residential_mf"],
        owner_name_keywords=["APARTMENTS", "APTS"],
    )

    parcel_ids = {
        r[0]
        for r in conn.execute("SELECT parcel_id FROM property_register").fetchall()
    }
    assert "P-STALE" not in parcel_ids, "Stale BMW Financial row survived re-import"
    # Real rows are present after clean-slate refresh
    assert "P1" in parcel_ids


def test_in_city_limits_flag(tmp_path: Path) -> None:
    """import_parcels sets in_city_limits=True for properties inside the polygon
    and in_city_limits=False for properties outside.

    Fixture polygon: 33.61 ≤ lat ≤ 33.65, -97.18 ≤ lon ≤ -97.10.
    Inside:  P1 (lat=33.626, lon=-97.133)
    Outside: P-OUT (lat=33.50, lon=-97.00)
    """
    klement = tmp_path / "klement.db"
    _make_klement_db(klement)

    geojson_path = tmp_path / "gainesville_test.geojson"
    _make_city_limits_geojson(geojson_path)
    # Clear the cache so the fresh tmp_path file is seen.
    load_polygon.cache_clear()

    conn = connect(tmp_path / "g.duckdb")
    apply_schema(conn)

    import_parcels(
        klement_db_path=klement,
        target_conn=conn,
        county_fips="48097",
        property_types=["residential_mf"],
        owner_name_keywords=["APARTMENTS", "APTS"],
        city_limits_polygon_path=geojson_path,
    )

    rows = {
        r[0]: r[1]
        for r in conn.execute(
            "SELECT parcel_id, in_city_limits FROM property_register"
        ).fetchall()
    }

    # P1: lat=33.626, lon=-97.133 → inside the box → True
    assert rows.get("P1") is True, f"P1 should be inside city limits; got {rows.get('P1')}"
    # P-OUT: lat=33.50, lon=-97.00 → outside the box → False
    assert rows.get("P-OUT") is False, (
        f"P-OUT should be outside city limits; got {rows.get('P-OUT')}"
    )


def test_in_city_limits_missing_polygon_returns_null(tmp_path: Path) -> None:
    """When no polygon path is provided, in_city_limits is NULL on every row."""
    klement = tmp_path / "klement.db"
    _make_klement_db(klement)
    conn = connect(tmp_path / "g.duckdb")
    apply_schema(conn)

    import_parcels(
        klement_db_path=klement,
        target_conn=conn,
        county_fips="48097",
        property_types=["residential_mf"],
        owner_name_keywords=["APARTMENTS", "APTS"],
        # city_limits_polygon_path intentionally omitted
    )

    rows = conn.execute(
        "SELECT in_city_limits FROM property_register"
    ).fetchall()
    assert rows, "Expected rows in property_register"
    for (val,) in rows:
        assert val is None, f"Expected NULL in_city_limits without polygon; got {val!r}"


# ---------------------------------------------------------------------------
# Geocoding fallback tests
# ---------------------------------------------------------------------------


def _make_klement_db_with_null_coords(path: Path) -> None:
    """Synthesize a klement.db where one residential_mf parcel has NULL lat/lon.

    P-NULL has NULL coords in klement; geocoding fallback should supply them.
    P-GOOD has valid coords and must not trigger geocoding.
    """
    k = sqlite3.connect(str(path))
    k.execute(
        "CREATE TABLE property ("
        "parcel_id TEXT, county TEXT, owner_name TEXT, property_address TEXT, "
        "city TEXT, zip TEXT, latitude REAL, longitude REAL, property_type TEXT, "
        "unit_count INTEGER, year_built INTEGER)"
    )
    k.executemany(
        "INSERT INTO property VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            # Inside the synthetic city-limits polygon after geocoding
            ("P-NULL", "cooke", "NULL COORDS LLC",
             "100 Main St", "Gainesville", "76240",
             None, None, "residential_mf", 10, 2000),
            # Valid coords already present — no geocoding needed
            ("P-GOOD", "cooke", "GOOD COORDS LLC",
             "200 Oak Ave", "Gainesville", "76240",
             33.626, -97.133, "residential_mf", 20, 2010),
        ],
    )
    k.commit()
    k.close()


def test_geocoding_fires_for_null_coords_and_sets_in_city_limits(tmp_path: Path) -> None:
    """When klement returns NULL lat/lon, geocode() is called and in_city_limits is set.

    The mock geocoder returns coords that land inside the test polygon, so
    in_city_limits must be True for P-NULL after import.
    """
    klement = tmp_path / "klement.db"
    _make_klement_db_with_null_coords(klement)

    geojson_path = tmp_path / "gainesville_test.geojson"
    _make_city_limits_geojson(geojson_path)
    load_polygon.cache_clear()

    conn = connect(tmp_path / "g.duckdb")
    apply_schema(conn)

    geocode_cache = tmp_path / ".geocode_cache.json"

    # Mock geocode to return coords inside the test polygon without network calls.
    with patch(
        "etl.gainesville.sources.cooke_cad_import.geocode",
        return_value=(33.626, -97.133),
    ) as mock_geocode:
        result = import_parcels(
            klement_db_path=klement,
            target_conn=conn,
            county_fips="48097",
            property_types=["residential_mf"],
            city_limits_polygon_path=geojson_path,
            geocode_cache_path=geocode_cache,
        )

    # geocode() must have been called exactly once (for P-NULL), not for P-GOOD.
    assert mock_geocode.call_count == 1
    call_args = mock_geocode.call_args
    assert "100 Main St" in call_args[0][0], (
        f"geocode called with unexpected address: {call_args[0][0]!r}"
    )
    assert call_args[1]["cache_path"] == geocode_cache

    rows = {
        r[0]: r[1]
        for r in conn.execute(
            "SELECT parcel_id, in_city_limits FROM property_register"
        ).fetchall()
    }

    # P-NULL was geocoded to inside coords → in_city_limits = True
    assert rows.get("P-NULL") is True, (
        f"P-NULL should be inside city limits after geocoding; got {rows.get('P-NULL')}"
    )
    # P-GOOD already had coords inside the polygon → in_city_limits = True
    assert rows.get("P-GOOD") is True, (
        f"P-GOOD should be inside city limits; got {rows.get('P-GOOD')}"
    )
    # Diagnostics should report 1 geocoded parcel
    assert result.diagnostics["geocoded"] == 1


def test_geocoding_failure_leaves_in_city_limits_null(tmp_path: Path) -> None:
    """When geocode() returns None (network failure / unknown address), in_city_limits stays NULL.
    """
    klement = tmp_path / "klement.db"
    _make_klement_db_with_null_coords(klement)

    geojson_path = tmp_path / "gainesville_test.geojson"
    _make_city_limits_geojson(geojson_path)
    load_polygon.cache_clear()

    conn = connect(tmp_path / "g.duckdb")
    apply_schema(conn)

    with patch(
        "etl.gainesville.sources.cooke_cad_import.geocode",
        return_value=None,
    ):
        import_parcels(
            klement_db_path=klement,
            target_conn=conn,
            county_fips="48097",
            property_types=["residential_mf"],
            city_limits_polygon_path=geojson_path,
            geocode_cache_path=tmp_path / ".geocode_cache.json",
        )

    rows = {
        r[0]: r[1]
        for r in conn.execute(
            "SELECT parcel_id, in_city_limits FROM property_register"
        ).fetchall()
    }

    # Geocoding returned None → in_city_limits must be NULL for P-NULL
    assert rows.get("P-NULL") is None, (
        f"P-NULL in_city_limits should be NULL when geocoding fails; got {rows.get('P-NULL')}"
    )


# ---------------------------------------------------------------------------
# parcel_id_exclusions tests
# ---------------------------------------------------------------------------


def test_parcel_id_exclusions_drops_sfr_parcel(tmp_path: Path) -> None:
    """A parcel listed in parcel_id_exclusions must NOT appear in property_register,
    even when its owner_name would normally match an owner_name_keywords pattern.

    P-SFR-EXCL has owner 'RICOCHET APARTMENTS LLC' (matches 'APARTMENTS') but its
    parcel_id is in the exclusion list — operator confirmed it is SFR.
    """
    klement = tmp_path / "klement.db"
    _make_klement_db(klement)
    conn = connect(tmp_path / "g.duckdb")
    apply_schema(conn)

    result = import_parcels(
        klement_db_path=klement,
        target_conn=conn,
        county_fips="48097",
        property_types=["residential_mf"],
        owner_name_keywords=["APARTMENTS", "APTS"],
        parcel_id_exclusions=["P-SFR-EXCL"],
    )

    assert result.status.value == "ok"
    parcel_ids = {
        r[0]
        for r in conn.execute("SELECT parcel_id FROM property_register").fetchall()
    }
    assert "P-SFR-EXCL" not in parcel_ids, (
        "P-SFR-EXCL (operator-confirmed SFR) leaked into property_register"
    )
    # Real MF properties still present
    assert {"P1", "P2", "P4", "P5", "P-OUT"}.issubset(parcel_ids), (
        f"Real MF parcels missing after exclusion; got {parcel_ids}"
    )


def test_parcel_id_exclusions_empty_list_no_change(tmp_path: Path) -> None:
    """Passing an empty exclusion list (or None) must produce the same result as
    not passing the parameter at all — no rows are dropped by the exclusion guard."""
    klement = tmp_path / "klement.db"
    _make_klement_db(klement)
    conn = connect(tmp_path / "g.duckdb")
    apply_schema(conn)

    result = import_parcels(
        klement_db_path=klement,
        target_conn=conn,
        county_fips="48097",
        property_types=["residential_mf"],
        owner_name_keywords=["APARTMENTS", "APTS"],
        parcel_id_exclusions=[],  # explicit empty list — must not drop anything extra
    )

    assert result.status.value == "ok"
    parcel_ids = {
        r[0]
        for r in conn.execute("SELECT parcel_id FROM property_register").fetchall()
    }
    # P-SFR-EXCL passes the keyword filter and no exclusion list is active → present
    assert "P-SFR-EXCL" in parcel_ids, (
        "P-SFR-EXCL should be included when exclusion list is empty"
    )
    assert result.diagnostics["total"] == 6  # P1, P2, P-OUT, P4, P5, P-SFR-EXCL


# ---------------------------------------------------------------------------
# Address GLOB '[0-9]*' filter tests (require street number)
# ---------------------------------------------------------------------------


def _make_klement_db_address_variants(path: Path) -> None:
    """Synthesize a klement.db with address-validity edge cases.

    Rows included:
      - PA-DIGIT: '123 Main St'                  → valid, starts with digit
      - PA-COMMA: ', Gainesville, Tx, 76240'     → AUTO-CHIOR style, NO digit at start → excluded
      - PA-NONUM: 'Gainesville Apartments'        → no digit at start → excluded
    """
    k = sqlite3.connect(str(path))
    k.execute(
        "CREATE TABLE property ("
        "parcel_id TEXT, county TEXT, owner_name TEXT, property_address TEXT, "
        "city TEXT, zip TEXT, latitude REAL, longitude REAL, property_type TEXT, "
        "unit_count INTEGER, year_built INTEGER)"
    )
    k.executemany(
        "INSERT INTO property VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            ("PA-DIGIT", "cooke", "DIGIT APTS LLC",
             "123 Main St", "Gainesville", "76240",
             33.626, -97.133, "residential_mf", 12, 2000),
            ("PA-COMMA", "cooke", "AUTO-CHIOR SERVICES LLC",
             ", Gainesville, Tx, 76240", "Gainesville", "76240",
             None, None, "residential_mf", None, None),
            ("PA-NONUM", "cooke", "GAINESVILLE APARTMENTS INC",
             "Gainesville Apartments", "Gainesville", "76240",
             None, None, "residential_mf", 8, 1995),
        ],
    )
    k.commit()
    k.close()


def test_address_comma_prefix_excluded(tmp_path: Path) -> None:
    """Row with property_address starting with ',' (AUTO-CHIOR style) must be excluded."""
    klement = tmp_path / "klement.db"
    _make_klement_db_address_variants(klement)
    conn = connect(tmp_path / "g.duckdb")
    apply_schema(conn)

    import_parcels(
        klement_db_path=klement,
        target_conn=conn,
        county_fips="48097",
        property_types=["residential_mf"],
        owner_name_keywords=["APARTMENTS", "APTS"],
    )

    parcel_ids = {
        r[0]
        for r in conn.execute("SELECT parcel_id FROM property_register").fetchall()
    }
    assert "PA-COMMA" not in parcel_ids, (
        "PA-COMMA (address starts with ',') should be excluded by GLOB '[0-9]*'"
    )


def test_address_no_street_number_excluded(tmp_path: Path) -> None:
    """Row with property_address that has no leading digit must be excluded."""
    klement = tmp_path / "klement.db"
    _make_klement_db_address_variants(klement)
    conn = connect(tmp_path / "g.duckdb")
    apply_schema(conn)

    import_parcels(
        klement_db_path=klement,
        target_conn=conn,
        county_fips="48097",
        property_types=["residential_mf"],
        owner_name_keywords=["APARTMENTS", "APTS"],
    )

    parcel_ids = {
        r[0]
        for r in conn.execute("SELECT parcel_id FROM property_register").fetchall()
    }
    assert "PA-NONUM" not in parcel_ids, (
        "PA-NONUM ('Gainesville Apartments', no leading digit) should be excluded by GLOB '[0-9]*'"
    )


def test_address_with_street_number_included(tmp_path: Path) -> None:
    """Row with property_address starting with a digit must pass through."""
    klement = tmp_path / "klement.db"
    _make_klement_db_address_variants(klement)
    conn = connect(tmp_path / "g.duckdb")
    apply_schema(conn)

    import_parcels(
        klement_db_path=klement,
        target_conn=conn,
        county_fips="48097",
        property_types=["residential_mf"],
        owner_name_keywords=["APARTMENTS", "APTS"],
    )

    parcel_ids = {
        r[0]
        for r in conn.execute("SELECT parcel_id FROM property_register").fetchall()
    }
    assert "PA-DIGIT" in parcel_ids, (
        "PA-DIGIT ('123 Main St') should be included — address starts with a digit"
    )
