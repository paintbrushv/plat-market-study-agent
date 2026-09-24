"""Query helpers for the monthly reports.

Pure DuckDB queries returning plain Python types (dicts, lists). Renderers
consume these and produce markdown.
"""

from __future__ import annotations

from typing import Any

import duckdb


def universe_summary(conn: duckdb.DuckDBPyConnection) -> dict[str, int]:
    row = conn.execute(
        "SELECT "
        "  COUNT(*) AS properties_total, "
        "  COALESCE(SUM(units), 0) AS units_total, "
        "  COALESCE(SUM(CASE WHEN in_city_limits THEN 1 ELSE 0 END), 0) AS properties_in_city, "
        "  COALESCE(SUM(CASE WHEN in_city_limits THEN units ELSE 0 END), 0) AS units_in_city "
        "FROM property_register"
    ).fetchone()
    return {
        "properties_total": int(row[0]),
        "units_total": int(row[1]),
        "properties_in_city": int(row[2]),
        "units_in_city": int(row[3]),
    }


def asking_rent_quartiles_by_beds(
    conn: duckdb.DuckDBPyConnection,
    *,
    status: str = "active",
    listing_kinds: tuple[str, ...] = ("mf", "duplex", "fourplex"),
) -> dict[float, dict[str, float | int]]:
    placeholders = ", ".join("?" * len(listing_kinds))
    rows = conn.execute(
        "SELECT beds, "
        "  COUNT(*) AS n, "
        "  QUANTILE_CONT(current_rent, 0.50) AS median_rent, "
        "  QUANTILE_CONT(current_rent, 0.25) AS p25_rent, "
        "  QUANTILE_CONT(current_rent, 0.75) AS p75_rent, "
        "  QUANTILE_CONT(current_rent::DOUBLE / NULLIF(sqft, 0), 0.50) AS median_psf "
        "FROM canonical_listings "
        f"WHERE status = ? AND current_rent IS NOT NULL AND beds IS NOT NULL "
        f"  AND listing_kind IN ({placeholders}) "
        "GROUP BY beds ORDER BY beds",
        [status, *listing_kinds],
    ).fetchall()
    return {
        float(row[0]): {
            "count": int(row[1]),
            "median": int(row[2]) if row[2] is not None else None,
            "p25": int(row[3]) if row[3] is not None else None,
            "p75": int(row[4]) if row[4] is not None else None,
            "median_psf": round(row[5], 2) if row[5] is not None else None,
        }
        for row in rows
    }


def owner_concentration_top_n(
    conn: duckdb.DuckDBPyConnection,
    *,
    n: int = 10,
) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT owner_entity_normalized AS owner, "
        "  COUNT(*) AS properties, "
        "  COALESCE(SUM(units), 0) AS units "
        "FROM property_register "
        "WHERE owner_entity_normalized IS NOT NULL AND owner_entity_normalized != '' "
        "GROUP BY owner_entity_normalized "
        "ORDER BY units DESC, properties DESC "
        "LIMIT ?",
        [n],
    ).fetchall()
    return [
        {"owner": r[0], "properties": int(r[1]), "units": int(r[2])}
        for r in rows
    ]


def listings_unmatched_to_register(conn: duckdb.DuckDBPyConnection) -> int:
    return int(
        conn.execute(
            "SELECT COUNT(*) FROM canonical_listings "
            "WHERE listing_kind IN ('mf', 'duplex', 'fourplex') "
            "AND property_id IS NULL AND status = 'active'"
        ).fetchone()[0]
    )


def coverage_diagnostics(
    conn: duckdb.DuckDBPyConnection,
    *,
    month_start: str,
    month_end: str,
) -> dict[str, Any]:
    obs_per_source = conn.execute(
        "SELECT source, COUNT(*) FROM raw_observations "
        "WHERE scraped_at >= ? AND scraped_at < ? "
        "GROUP BY source ORDER BY source",
        [month_start, month_end],
    ).fetchall()
    failed = conn.execute(
        "SELECT run_id, sources_failed FROM run_log "
        "WHERE started_at >= ? AND started_at < ? AND len(sources_failed) > 0",
        [month_start, month_end],
    ).fetchall()
    review_open = int(
        conn.execute(
            "SELECT COUNT(*) FROM dedup_review WHERE decided = FALSE"
        ).fetchone()[0]
    )
    return {
        "obs_per_source": {r[0]: int(r[1]) for r in obs_per_source},
        "failed_runs": [{"run_id": r[0], "failed": list(r[1])} for r in failed],
        "review_queue_open": review_open,
    }


def zori_latest(
    conn: duckdb.DuckDBPyConnection,
    zip_code: str,
    home_type: str = "all",
) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT month, zori_value FROM zori_history "
        "WHERE zip = ? AND home_type = ? "
        "ORDER BY month DESC LIMIT 1",
        [zip_code, home_type],
    ).fetchone()
    if not row:
        return None
    yoy = conn.execute(
        "SELECT zori_value FROM zori_history "
        "WHERE zip = ? AND home_type = ? AND month = ?",
        [zip_code, home_type, row[0].replace(year=row[0].year - 1)],
    ).fetchone()
    return {
        "month": row[0].isoformat(),
        "value": row[1],
        "yoy_pct": ((row[1] / yoy[0]) - 1) * 100 if yoy and yoy[0] else None,
    }


def sfr_local_median(
    conn: duckdb.DuckDBPyConnection,
    *,
    status: str = "active",
) -> dict[str, Any]:
    row = conn.execute(
        "SELECT COUNT(*) AS n, "
        "  QUANTILE_CONT(current_rent, 0.50) AS median_rent, "
        "  QUANTILE_CONT(current_rent::DOUBLE / NULLIF(sqft, 0), 0.50) AS median_psf "
        "FROM canonical_listings "
        "WHERE listing_kind = 'sfr' AND status = ? AND current_rent IS NOT NULL",
        [status],
    ).fetchone()
    return {
        "count": int(row[0]),
        "median_rent": int(row[1]) if row[1] is not None else None,
        "median_psf": round(row[2], 2) if row[2] is not None else None,
    }
