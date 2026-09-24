"""Cooke County CAD parcels — imported from klement's raw property table.

We do NOT consume klement's entity-match output. We read the raw,
unfiltered property table and apply our own multifamily type filter.
If north_tx_cad.db is missing or empty, we log and exit OK (property register
goes stale, monthly report says so). If required columns are absent we
raise KlementSchemaMismatch — that's a deliberate fail-fast moment
requiring a conscious fix on both sides.

Schema note: klement uses SQLite (not DuckDB). The property table has
property_type enum residential_sf|residential_mf|commercial|industrial|
land|agricultural|other. Some MF properties are misclassified as 'other'
by CCAD, so we also catch rows where owner_name contains "APARTMENTS"
or "APTS".
"""

from __future__ import annotations

import datetime as dt
import hashlib
import logging
import sqlite3
from pathlib import Path

import duckdb

from etl.gainesville.dataclasses import CollectionResult, CollectionStatus
from etl.gainesville.geo import load_polygon, point_in_polygon
from etl.gainesville.geocode import geocode
from etl.gainesville.owner_normalize import normalize_owner

logger = logging.getLogger(__name__)

_DEFAULT_REQUIRED_COLUMNS = [
    "parcel_id",
    "county",
    "owner_name",
    "property_address",
    "city",
    "zip",
    "latitude",
    "longitude",
    "property_type",
    "unit_count",
    "year_built",
]


class KlementSchemaMismatch(RuntimeError):
    pass


_DEFAULT_GEOCODE_CACHE_PATH = Path("data/local/.geocode_cache.json")


def import_parcels(
    *,
    klement_db_path: Path,
    target_conn: duckdb.DuckDBPyConnection,
    county_fips: str,
    property_types: list[str] | None = None,
    owner_name_keywords: list[str] | None = None,
    required_columns: list[str] | None = None,
    city_limits_polygon_path: Path | None = None,
    geocode_cache_path: Path | None = None,
    parcel_id_exclusions: list[str] | None = None,
    # Legacy params — accepted but ignored; kept for call-site compatibility
    class_codes: list[str] | None = None,
    expected_schema_version: int | None = None,
) -> CollectionResult:
    """Import MF parcels from north_tx_cad.db into the local property_register.

    Args:
        klement_db_path: Path to klement's SQLite database.
        target_conn: DuckDB connection for the local property register.
        county_fips: FIPS code for the target county (used for diagnostics).
        property_types: List of klement property_type values to include.
            Defaults to ["residential_mf"].
        owner_name_keywords: Additional UPPER(owner_name) LIKE patterns to
            catch CCAD-misclassified MF (e.g. ["APARTMENTS", "APTS"]).
        required_columns: Column names that must exist in klement's property
            table. Raises KlementSchemaMismatch if any are absent.
        city_limits_polygon_path: Optional path to a GeoJSON FeatureCollection
            containing the city-limits polygon. When provided, each property's
            lat/lon is tested and ``in_city_limits`` is set accordingly. NULL
            when the file is missing or the property has no coordinates.
        geocode_cache_path: Path to the JSON geocode cache file used for
            Nominatim fallback lookups. When None, defaults to
            ``data/local/.geocode_cache.json``. Any parcel where klement
            returns NULL or zero lat/lon is looked up via Nominatim; the result
            is cached so repeat quarterly runs avoid re-hitting the API.
        parcel_id_exclusions: Operator-confirmed non-MF parcels to exclude even
            if they pass the property_type / owner_name filters. Typically used
            for SFR properties whose LLC name contains "APARTMENTS". When None
            or empty, no extra exclusions are applied.
        class_codes: Ignored. Accepted for backwards-compat with old callers.
        expected_schema_version: Ignored. klement has no schema_version table;
            column-existence check is used instead.
    """
    if property_types is None:
        property_types = ["residential_mf"]
    if owner_name_keywords is None:
        owner_name_keywords = []
    if required_columns is None:
        required_columns = _DEFAULT_REQUIRED_COLUMNS
    if geocode_cache_path is None:
        geocode_cache_path = _DEFAULT_GEOCODE_CACHE_PATH
    if parcel_id_exclusions is None:
        parcel_id_exclusions = []

    if not klement_db_path.exists() or klement_db_path.stat().st_size == 0:
        logger.warning(
            "north_tx_cad.db not found or empty at %s; property register stays stale",
            klement_db_path,
        )
        return CollectionResult(
            [],
            CollectionStatus.OK,
            {"klement_unavailable": str(klement_db_path)},
        )

    uri = "file:" + str(klement_db_path) + "?mode=ro"
    k = sqlite3.connect(uri, uri=True)
    try:
        # Verify required columns via PRAGMA
        pragma_rows = k.execute("PRAGMA table_info(property)").fetchall()
        existing_cols = {row[1] for row in pragma_rows}
        missing = [c for c in required_columns if c not in existing_cols]
        if missing:
            raise KlementSchemaMismatch(
                f"klement property table missing required columns: {missing!r} "
                f"(found: {sorted(existing_cols)!r})"
            )

        # Build WHERE clause
        type_placeholders = ", ".join("?" * len(property_types))
        # Owner-name keyword sub-clauses (e.g. APARTMENTS, APTS) — each keyword
        # must also pass the personal-property-lease exclusion guards.
        if owner_name_keywords:
            kw_inner_clauses = " OR ".join(
                f"UPPER(owner_name) LIKE ?" for _ in owner_name_keywords
            )
            type_or_kw = (
                f"(property_type IN ({type_placeholders}) OR "
                f"(({kw_inner_clauses}) "
                f"AND UPPER(owner_name) NOT LIKE '%FINANCIAL%' "
                f"AND UPPER(owner_name) NOT LIKE '%LEASING%'))"
            )
        else:
            type_or_kw = f"property_type IN ({type_placeholders})"

        # Operator-confirmed exclusions: SFR parcels whose owner names match
        # apartment indicators. Applied as an explicit NOT IN guard.
        if parcel_id_exclusions:
            excl_placeholders = ", ".join("?" * len(parcel_id_exclusions))
            excl_clause = f"AND parcel_id NOT IN ({excl_placeholders}) "
        else:
            excl_clause = ""

        where = (
            f"WHERE LOWER(county) = 'cooke' "
            f"AND property_address IS NOT NULL "
            f"AND TRIM(property_address) != '' "
            f"AND property_address GLOB '[0-9]*' "
            f"AND {type_or_kw} "
            f"{excl_clause}"
            f"AND UPPER(owner_name) NOT LIKE '%FINANCIAL%' "
            f"AND UPPER(owner_name) NOT LIKE '%LEASING LLC%' "
            f"AND UPPER(owner_name) NOT LIKE '%LEASE TRUST%' "
            f"AND UPPER(owner_name) NOT LIKE '%DONLEN%' "
            f"AND UPPER(owner_name) NOT LIKE '%DIRECTV%' "
            f"AND UPPER(owner_name) NOT LIKE '%DISH NETWORK%' "
            f"AND UPPER(owner_name) NOT LIKE '%PITNEY BOWES%' "
            f"AND UPPER(owner_name) NOT LIKE '%ADT %'"
        )

        params: list[str] = list(property_types) + [
            f"%{kw}%" for kw in owner_name_keywords
        ] + list(parcel_id_exclusions)

        rows = k.execute(
            f"SELECT parcel_id, owner_name, property_address, city, zip, "
            f"latitude, longitude, property_type, unit_count, year_built "
            f"FROM property {where}",
            params,
        ).fetchall()
    finally:
        k.close()

    now = dt.datetime.now(dt.UTC)

    # Load city-limits polygon once (cached); None if path missing or not provided.
    poly = load_polygon(city_limits_polygon_path) if city_limits_polygon_path else None

    # Full refresh: delete all existing cooke_cad rows so stale personal-property
    # leases (and any other previously-matched-but-now-filtered rows) don't linger.
    # Must cascade through canonical_listings first to avoid FK constraint violation.
    target_conn.execute(
        """
        DELETE FROM canonical_listings
        WHERE property_id IN (
            SELECT property_id FROM property_register
            WHERE source_of_truth = 'cooke_cad'
        )
        """
    )
    target_conn.execute(
        "DELETE FROM property_register WHERE source_of_truth = 'cooke_cad'"
    )

    n_inserted = 0
    n_updated = 0

    n_geocoded = 0
    for parcel_id, owner, addr, city, zip_, lat, lon, prop_type, units, year_built in rows:
        # Geocoding fallback: klement returns NULL coords for some Cooke parcels.
        # Without coords the spatial check always returns NULL and in_city_limits
        # is never populated, even though the quarterly DELETE+re-insert would
        # otherwise reset any manually-patched values.  We look up missing coords
        # via Nominatim (rate-limited, cached) so the spatial check is durable.
        if not lat or not lon:
            # klement's property_address already includes "City, Tx, Zip" so use
            # it verbatim; appending city/state/zip again produces a double-suffix
            # that Nominatim rejects (e.g. "719 S Weaver, Gainesville, Tx, 76240,
            # Gainesville, TX 76240" → no results).
            query = addr
            coords = geocode(query, cache_path=geocode_cache_path)
            if coords is not None:
                lat, lon = coords
                n_geocoded += 1
                logger.debug("Geocoded %s → (%.6f, %.6f)", parcel_id, lat, lon)
            else:
                logger.warning("Could not geocode parcel %s at %r", parcel_id, query)

        property_id = "p_" + hashlib.sha1(parcel_id.encode("utf-8")).hexdigest()[:12]
        property_type = _classify_size(prop_type, units)
        owner_norm = normalize_owner(owner)
        in_city = point_in_polygon(lat, lon, poly)

        existing = target_conn.execute(
            "SELECT 1 FROM property_register WHERE property_id = ?", [property_id]
        ).fetchone()

        if existing:
            target_conn.execute(
                "UPDATE property_register SET owner_name_raw=?, owner_entity_normalized=?, "
                "address=?, city=?, zip=?, lat=?, lon=?, units=?, year_built=?, "
                "property_type=?, in_city_limits=?, last_updated=?, source_of_truth='cooke_cad' "
                "WHERE property_id=?",
                [owner, owner_norm, addr, city, zip_, lat, lon, units, year_built,
                 property_type, in_city, now, property_id],
            )
            n_updated += 1
        else:
            # 18 columns: property_id, parcel_id, name, address, city, zip,
            # in_city_limits, lat, lon, property_type, units, year_built,
            # owner_name_raw, owner_entity_normalized, first_seen, last_updated,
            # source_of_truth, notes
            target_conn.execute(
                "INSERT INTO property_register VALUES "
                "(?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'cooke_cad', NULL)",
                [property_id, parcel_id, addr, city, zip_, in_city, lat, lon,
                 property_type, units, year_built, owner, owner_norm, now, now],
            )
            n_inserted += 1

    return CollectionResult(
        [],
        CollectionStatus.OK,
        {
            "inserted": n_inserted,
            "updated": n_updated,
            "total": n_inserted + n_updated,
            "geocoded": n_geocoded,
        },
    )


def _classify_size(property_type: str, units: int | None) -> str:
    """Map klement property_type + unit count to our internal MF size bucket."""
    u = units or 0
    if u >= 50:
        return "mf-large"
    if u >= 5:
        return "mf-small"
    if u == 4:
        return "fourplex"
    if u == 2:
        return "duplex"
    # unit_count NULL or ambiguous — default to mf-small for residential_mf
    return "mf-small"
