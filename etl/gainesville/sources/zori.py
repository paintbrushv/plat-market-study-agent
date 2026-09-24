"""ZORI (Zillow Observed Rent Index) ingester.

Public CSV at:
    https://files.zillowstatic.com/research/public_csvs/zori/...

The published file is wide (one column per month). We melt it long, filter
to in-catchment ZIPs, and upsert into zori_history.
"""

from __future__ import annotations

import datetime as dt
import logging
import urllib.error
import urllib.request
from pathlib import Path
from typing import Final

import duckdb

from etl.gainesville.dataclasses import Catchment, CollectionResult, CollectionStatus

logger = logging.getLogger(__name__)

ZORI_URL_ALL: Final[str] = (
    "https://files.zillowstatic.com/research/public_csvs/zori/"
    "Zip_zori_uc_sfrcondomfr_sm_month.csv"
)
ZORI_URL_SFR: Final[str] = (
    "https://files.zillowstatic.com/research/public_csvs/zori/"
    "Zip_zori_uc_sfr_sm_month.csv"
)

URL_BY_HOME_TYPE = {"all": ZORI_URL_ALL, "sfr": ZORI_URL_SFR}


def collect_from_url(
    catchment: Catchment,
    conn: duckdb.DuckDBPyConnection,
    home_type: str = "all",
) -> CollectionResult:
    """Download ZORI CSV from Zillow and ingest. Used in production."""
    url = URL_BY_HOME_TYPE[home_type]
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "market-study-agent/1.0 (Gainesville TX listings)"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
            data = resp.read()
    except (urllib.error.URLError, TimeoutError) as e:
        logger.error("ZORI download failed: %s", e)
        return CollectionResult([], CollectionStatus.NETWORK_FAILED, {"error": str(e)})
    tmp = Path("/tmp") / f"zori_{home_type}_{dt.date.today()}.csv"
    tmp.write_bytes(data)
    return collect_from_csv(tmp, catchment, conn, home_type)


def collect_from_csv(
    csv_path: Path,
    catchment: Catchment,
    conn: duckdb.DuckDBPyConnection,
    home_type: str,
) -> CollectionResult:
    """Read a ZORI CSV file and upsert in-catchment rows. Pure (no network)."""
    fetched_at = dt.datetime.now(dt.UTC)
    zip_filter = list(catchment.zip_codes)
    rows = conn.execute(
        """
        WITH src AS (
            SELECT * FROM read_csv_auto(?)
        ),
        long AS (
            SELECT
                CAST(RegionName AS VARCHAR) AS zip,
                unpivot_col AS month_str,
                CAST(unpivot_val AS DOUBLE) AS zori_value
            FROM src
            UNPIVOT (
                unpivot_val FOR unpivot_col IN (
                    COLUMNS(c -> regexp_full_match(c, '^\\d{{4}}-\\d{{2}}-\\d{{2}}$'))
                )
            )
        )
        SELECT zip, CAST(month_str AS DATE) AS month, zori_value
        FROM long
        WHERE zip IN ({})
          AND zori_value IS NOT NULL
        """.format(", ".join("?" * len(zip_filter))),
        [str(csv_path), *zip_filter],
    ).fetchall()
    n = 0
    for zip_code, month, value in rows:
        conn.execute(
            """
            INSERT INTO zori_history (zip, month, zori_value, home_type, fetched_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (zip, month, home_type) DO UPDATE SET
                zori_value = EXCLUDED.zori_value,
                fetched_at = EXCLUDED.fetched_at
            """,
            [zip_code, month, value, home_type, fetched_at],
        )
        n += 1
    return CollectionResult([], CollectionStatus.OK, {"rows_upserted": n})
