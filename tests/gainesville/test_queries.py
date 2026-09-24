from __future__ import annotations

import datetime as dt
from pathlib import Path

import duckdb
from etl.gainesville.db import apply_schema, connect
from etl.gainesville.queries import (
    asking_rent_quartiles_by_beds,
    owner_concentration_top_n,
    universe_summary,
)


def _seed_property(conn: duckdb.DuckDBPyConnection, **kwargs: object) -> None:
    defaults = dict(
        property_id="p1", parcel_id="P1", name="A", address="100 Main",
        city="Gainesville", zip="76240", in_city_limits=True, lat=None, lon=None,
        property_type="mf-large", units=80, year_built=1990,
        owner_name_raw="ABC LLC", owner_entity_normalized="abc",
        first_seen=dt.datetime.now(dt.UTC),
        last_updated=dt.datetime.now(dt.UTC),
        source_of_truth="cooke_cad", notes=None,
    )
    defaults.update(kwargs)
    conn.execute(
        "INSERT INTO property_register VALUES "
        "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [defaults[k] for k in [
            "property_id", "parcel_id", "name", "address", "city", "zip",
            "in_city_limits", "lat", "lon", "property_type", "units", "year_built",
            "owner_name_raw", "owner_entity_normalized", "first_seen",
            "last_updated", "source_of_truth", "notes",
        ]],
    )


def _seed_canonical(
    conn: duckdb.DuckDBPyConnection, canonical_id: str, beds: float, rent: int, sqft: int = 900
) -> None:
    today = dt.date.today()
    conn.execute(
        "INSERT INTO canonical_listings VALUES "
        "(?, NULL, 'mf', '100 main', ?, 1.0, ?, ?, ?, 0, 'active', ?, ?, ?, "
        "['apartments_com'], ['o1'], 1.0, FALSE)",
        [canonical_id, beds, sqft, today, today, rent, rent, rent],
    )


def test_universe_summary_counts(tmp_path: Path) -> None:
    conn = connect(tmp_path / "g.duckdb")
    apply_schema(conn)
    _seed_property(conn, property_id="p1", units=80, in_city_limits=True)
    _seed_property(conn, property_id="p2", units=12, in_city_limits=False)
    _seed_property(conn, property_id="p3", units=4, in_city_limits=True)
    summary = universe_summary(conn)
    assert summary["properties_total"] == 3
    assert summary["units_total"] == 96
    assert summary["properties_in_city"] == 2
    assert summary["units_in_city"] == 84


def test_asking_rent_quartiles_by_beds(tmp_path: Path) -> None:
    conn = connect(tmp_path / "g.duckdb")
    apply_schema(conn)
    for i, rent in enumerate([1000, 1100, 1200, 1300, 1400]):
        _seed_canonical(conn, f"c{i}", beds=2.0, rent=rent)
    quartiles = asking_rent_quartiles_by_beds(conn)
    two = quartiles[2.0]
    assert two["count"] == 5
    assert two["median"] == 1200
    assert two["p25"] == 1100
    assert two["p75"] == 1300


def test_asking_rent_quartiles_excludes_sfr(tmp_path: Path) -> None:
    """Default listing_kinds=(mf, duplex, fourplex) must exclude sfr rows."""
    conn = connect(tmp_path / "g.duckdb")
    apply_schema(conn)
    today = dt.date.today()
    # 3 SFR listings at high rents
    for i, rent in enumerate([2000, 2100, 2200]):
        conn.execute(
            "INSERT INTO canonical_listings VALUES "
            "(?, NULL, 'sfr', ?, 3.0, 2.0, 1200, ?, ?, 0, 'active', ?, ?, ?, "
            "['craigslist'], ['o_sfr'], 1.0, FALSE)",
            [f"sfr_{i}", f"{i} sfr lane", today, today, rent, rent, rent],
        )
    # 3 MF listings at lower rents
    for i, rent in enumerate([1000, 1100, 1200]):
        conn.execute(
            "INSERT INTO canonical_listings VALUES "
            "(?, NULL, 'mf', ?, 3.0, 2.0, 900, ?, ?, 0, 'active', ?, ?, ?, "
            "['apartments_com'], ['o_mf'], 1.0, FALSE)",
            [f"mf_{i}", f"{i} mf court", today, today, rent, rent, rent],
        )
    quartiles = asking_rent_quartiles_by_beds(conn)
    assert 3.0 in quartiles
    three = quartiles[3.0]
    assert three["count"] == 3
    assert three["median"] == 1100  # MF median, not SFR


def test_owner_concentration_top_n(tmp_path: Path) -> None:
    conn = connect(tmp_path / "g.duckdb")
    apply_schema(conn)
    _seed_property(conn, property_id="p1", units=100, owner_entity_normalized="big landlord")
    _seed_property(conn, property_id="p2", units=80, owner_entity_normalized="big landlord")
    _seed_property(conn, property_id="p3", units=50, owner_entity_normalized="small")
    top = owner_concentration_top_n(conn, n=2)
    assert top[0]["owner"] == "big landlord"
    assert top[0]["units"] == 180
    assert top[0]["properties"] == 2
    assert top[1]["owner"] == "small"
