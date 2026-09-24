"""Ingest BLS QCEW MSA-level employment and wages.

QCEW (Quarterly Census of Employment and Wages) is the universe count of
covered employment and wages, published quarterly with a ~5-6 month lag.
This is the metro-level signal referenced in the deviation watch:
employment-growth and wage-growth actuals to compare against CoStar's
forecast for each metro on the capital-deployment shortlist.

API: BLS QCEW Open Data, CSV slices (no key required).
  https://data.bls.gov/cew/doc/access/csv_data_slices.htm
  Endpoint: https://data.bls.gov/cew/data/api/{year}/{qtr}/area/{area}.csv

Default cuts: industry=10 (Total, all industries); ownership=0 (total covered)
and 5 (private). Override with --industries / --ownerships.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import io
import json
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, cast

pd: Any | None
try:
    import pandas as _pd
except ImportError:  # pragma: no cover - optional dependency
    pd = None
else:  # pragma: no cover - optional dependency
    pd = cast(Any, _pd)

QCEW_QUARTERLY_URL = "https://data.bls.gov/cew/data/api/{year}/{qtr}/area/{area}.csv"

DEFAULT_INDUSTRY = "10"  # Total, all industries
DEFAULT_OWNERSHIPS = ("0", "5")  # Total covered + private

OWNERSHIP_LABELS = {
    "0": "total_covered",
    "1": "federal_govt",
    "2": "state_govt",
    "3": "local_govt",
    "5": "private",
}

METRIC_FIELD_MAP = {
    "qcew_employment_month3": "month3_emplvl",
    "qcew_avg_weekly_wage": "avg_wkly_wage",
    "qcew_total_quarterly_wages": "total_qtrly_wages",
    "qcew_qtrly_estabs": "qtrly_estabs",
}


def cbsa_to_qcew_area(code: str) -> str:
    """Convert a CBSA code to QCEW MSA area FIPS.

    CBSA codes are 5 digits with a trailing 0 (e.g., 19100 = Dallas-Fort Worth).
    QCEW MSA codes are 'C' + first 4 digits (e.g., 'C1910'). Already-formatted
    codes ('C1910') pass through unchanged.
    """
    cleaned = str(code).strip().upper()
    if cleaned.startswith("C") and len(cleaned) == 5 and cleaned[1:].isdigit():
        return cleaned
    if cleaned.isdigit() and len(cleaned) == 5 and cleaned.endswith("0"):
        return f"C{cleaned[:4]}"
    raise ValueError(
        f"Cannot convert {code!r} to QCEW MSA area code "
        f"(expected 5-digit CBSA ending in 0, or 'CNNNN' format)"
    )


def fetch_quarter(area_fips: str, year: int, qtr: int) -> list[dict[str, Any]]:
    """Fetch all industry/ownership rows for an MSA-quarter from QCEW Open Data API.

    Returns an empty list if the quarter is not yet published or if the area-quarter
    has no data. Raises on network errors so the caller can decide whether to retry.
    """
    url = QCEW_QUARTERLY_URL.format(year=year, qtr=qtr, area=area_fips)
    req = urllib.request.Request(url, headers={"User-Agent": "market-study-agent QCEW ingest"})
    try:
        with urllib.request.urlopen(req) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as err:
        if err.code in {404, 410}:
            return []
        raise
    reader = csv.DictReader(io.StringIO(body))
    return [dict(row) for row in reader]


def filter_records(
    raw: list[dict[str, Any]],
    industries: tuple[str, ...] = (DEFAULT_INDUSTRY,),
    ownerships: tuple[str, ...] = DEFAULT_OWNERSHIPS,
) -> list[dict[str, Any]]:
    industries_set = {str(i) for i in industries}
    ownerships_set = {str(o) for o in ownerships}
    return [
        r
        for r in raw
        if str(r.get("industry_code")) in industries_set
        and str(r.get("own_code")) in ownerships_set
    ]


def normalize_records(
    rows: list[dict[str, Any]],
    msa_code: str,
    area_fips: str,
    year: int,
    qtr: int,
    source_id: str,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    as_of = f"{year}-Q{qtr}"
    for row in rows:
        ownership = OWNERSHIP_LABELS.get(str(row.get("own_code")), str(row.get("own_code")))
        industry_code = str(row.get("industry_code"))
        for metric, source_field in METRIC_FIELD_MAP.items():
            raw_val = row.get(source_field)
            if raw_val in (None, "", "."):
                continue
            try:
                value = float(raw_val)
            except (TypeError, ValueError):
                continue
            out.append(
                {
                    "metric": metric,
                    "value": value,
                    "as_of": as_of,
                    "year": year,
                    "quarter": qtr,
                    "ownership": ownership,
                    "industry_code": industry_code,
                    "geo_level": "msa",
                    "msa_code": str(msa_code),
                    "area_fips": area_fips,
                    "source_id": source_id,
                }
            )
    return out


def fetch_msa_panel(
    msa_code: str,
    start_year: int,
    end_year: int,
    industries: tuple[str, ...] = (DEFAULT_INDUSTRY,),
    ownerships: tuple[str, ...] = DEFAULT_OWNERSHIPS,
    sleep_seconds: float = 0.25,
    source_id: str = "BLS-QCEW",
) -> list[dict[str, Any]]:
    """Pull every published quarter in [start_year, end_year] for one MSA."""
    area_fips = cbsa_to_qcew_area(msa_code)
    today = dt.date.today()
    records: list[dict[str, Any]] = []
    for year in range(int(start_year), int(end_year) + 1):
        for qtr in (1, 2, 3, 4):
            quarter_end = dt.date(year, qtr * 3, 28)
            if quarter_end > today:
                continue
            raw = fetch_quarter(area_fips, year, qtr)
            if not raw:
                continue
            filtered = filter_records(raw, industries=industries, ownerships=ownerships)
            records.extend(normalize_records(filtered, msa_code, area_fips, year, qtr, source_id))
            if sleep_seconds:
                time.sleep(sleep_seconds)
    return records


def write_output(records: list[dict[str, Any]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if pd is None:
        with path.with_suffix(".jsonl").open("w", encoding="utf-8") as handle:
            for row in records:
                handle.write(json.dumps(row) + "\n")
        return
    assert pd is not None
    frame = pd.DataFrame.from_records(records)
    frame.to_parquet(path, index=False)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "msa_code",
        help="CBSA code (5 digits, e.g., 19100) or QCEW area code (e.g., C1910).",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Output parquet path. Default: data/public/processed/macro/qcew_<msa_code>.parquet",
    )
    parser.add_argument("--start-year", type=int, default=2015)
    parser.add_argument("--end-year", type=int, default=dt.date.today().year)
    parser.add_argument(
        "--industries",
        nargs="+",
        default=[DEFAULT_INDUSTRY],
        help="NAICS industry codes to keep (default: 10 = total, all industries).",
    )
    parser.add_argument(
        "--ownerships",
        nargs="+",
        default=list(DEFAULT_OWNERSHIPS),
        help="Ownership codes: 0=total covered, 5=private, 1/2/3=govt. Default: 0 5.",
    )
    parser.add_argument("--sleep-seconds", type=float, default=0.25)
    parser.add_argument("--source-id", default="BLS-QCEW")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    output_path = (
        Path(args.output)
        if args.output
        else Path(f"data/public/processed/macro/qcew_{args.msa_code}.parquet")
    )
    records = fetch_msa_panel(
        msa_code=args.msa_code,
        start_year=args.start_year,
        end_year=args.end_year,
        industries=tuple(args.industries),
        ownerships=tuple(args.ownerships),
        sleep_seconds=args.sleep_seconds,
        source_id=args.source_id,
    )
    write_output(records, output_path)
    print(f"Wrote {len(records)} records to {output_path}")


if __name__ == "__main__":
    main()
