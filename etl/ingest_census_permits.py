"""Ingest MSA-level building permits, via FRED's redistribution of Census BPS.

The original implementation called ``api.census.gov/data/timeseries/bps/permits``,
which has been 404 for some time (Census reorganized their construction-stats
distribution; see research_sandbox/rent_growth_forecast/RUNBOOK.md).

FRED republishes Census BPS in two shapes that this module dispatches between:

1. **Monthly SA MSA series** (``CBSA_TO_FRED``): one series per MSA, monthly
   seasonally-adjusted, 1988+. Used for the larger metros. Output: legacy
   schema (``metric=permits_total``, ``as_of="YYYY-MM"``) at
   ``data/public/processed/macro/census_permits_<cbsa>.parquet``.

2. **Annual NSA county-summed fallback** (``CBSA_TO_FRED_ANNUAL_COUNTIES``):
   for the smaller MSAs that FRED does not publish at MSA level, sum the
   annual NSA county series that compose the MSA. Output: distinct schema
   (``metric=permits_total_annual_nsa``, ``as_of="YYYY"``,
   ``frequency="annual"``, ``adjustment="NSA"``) at
   ``data/public/processed/macro/permits_annual_nsa_<cbsa>.parquet`` —
   intentionally a separate file so consumers don't accidentally mix
   monthly-SA with annual-NSA values.

CLI signature and the monthly-SA output schema are preserved so downstream
callers (``run_model_overlay.py``, ``pull_external_drivers.py``) keep
working untouched. The annual fallback is opt-in via the CBSA being in the
annual lookup; supported metros that fall through both lookups exit clean
with a warning.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import io
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, cast

import requests

pd: Any | None
try:
    import pandas as _pd
except ImportError:  # pragma: no cover - optional dependency
    pd = None
else:  # pragma: no cover - optional dependency
    pd = cast(Any, _pd)

FRED_GRAPH_CSV = "https://fred.stlouisfed.org/graph/fredgraph.csv"

# CBSA -> FRED series id for "New Privately-Owned Housing Units Authorized in
# Permit-Issuing Places: Total Units" (BPPRIVSA = seasonally adjusted).
# Discovered via fred.stlouisfed.org/searchresults/ on 2026-05-08.
# Note for CBSA 19100 (DFW): FRED publishes the two Metropolitan Divisions
# separately. DALL148 = Dallas-Plano-Irving MD, FORT148 = Fort Worth-Arlington MD.
# Sum the two if you need the full DFW MSA total.
CBSA_TO_FRED = {
    "12420": "AUST448BPPRIVSA",  # Austin-Round Rock-Georgetown, TX
    "13820": "BIRM801BPPRIVSA",  # Birmingham-Hoover, AL
    "19100": "DALL148BPPRIVSA",  # Dallas-Plano-Irving, TX (MD of DFW)
    "33260": "MIDL248BPPRIVSA",  # Midland, TX
    "41700": "SANA748BPPRIVSA",  # San Antonio-New Braunfels, TX
}

# CBSA -> annual NSA county-level series for MSAs not on FRED's MSA permit list.
# These are summed across constituent counties to approximate the MSA total.
# Annual frequency, NSA — output to a SEPARATE parquet so this never mixes with
# the monthly-SA MSA data above. ``coverage_note`` flags counties FRED does
# not publish (the resulting MSA total is an undercount).
# Discovered via fred.stlouisfed.org/searchresults/ on 2026-05-08.
CBSA_TO_FRED_ANNUAL_COUNTIES: dict[str, dict[str, Any]] = {
    "13140": {  # Beaumont-Port Arthur, TX = Hardin + Jefferson + Newton + Orange
        "series": ["BPPRIV048199", "BPPRIV048245", "BPPRIV048361"],
        "coverage_note": (
            "Newton County (FIPS 048351) not published on FRED; "
            "MSA total undercounted"
        ),
    },
    "26620": {  # Huntsville, AL = Limestone + Madison
        "series": ["BPPRIV001083", "BPPRIV001089"],
        "coverage_note": None,
    },
    "43300": {  # Sherman-Denison, TX = Grayson (single-county MSA)
        "series": ["BPPRIV048181"],
        "coverage_note": None,
    },
}


def fetch_permits(msa_code: str, start_year: int, end_year: int) -> list[dict[str, Any]]:
    """Fetch monthly permit observations for one MSA from FRED.

    Returns rows shaped like the legacy Census-API output so callers don't break:
    ``{"permit": str, "year": str, "month": str}``.

    Raises ``KeyError`` if the MSA isn't in CBSA_TO_FRED — caller decides whether
    to swallow (multi-metro loop) or surface (single-metro user-driven run).
    """
    msa = str(msa_code).strip()
    if msa not in CBSA_TO_FRED:
        raise KeyError(msa)
    series_id = CBSA_TO_FRED[msa]
    # FRED's edge appears to rate-limit / blackhole unrecognized User-Agents:
    # Python urllib's default and "market-study-agent/1.0" both stall, but a
    # curl-style UA returns 200 immediately. Keep this curl-style UA unless
    # FRED's behavior changes.
    response = requests.get(
        FRED_GRAPH_CSV,
        params={"id": series_id},
        headers={"User-Agent": "curl/8.6.0"},
        timeout=30,
    )
    response.raise_for_status()
    body = response.text

    reader = csv.DictReader(io.StringIO(body))
    rows: list[dict[str, Any]] = []
    for row in reader:
        raw_value = str(row.get(series_id, "") or "").strip()
        if raw_value in {"", "."}:
            continue
        # observation_date is YYYY-MM-DD; legacy schema wants year/month split.
        date = str(row.get("observation_date", "")).strip()
        if len(date) < 7:
            continue
        year = int(date[:4])
        if year < start_year or year > end_year:
            continue
        month = int(date[5:7])
        try:
            permit = float(raw_value)
        except ValueError:
            continue
        rows.append(
            {
                "permit": permit,
                "year": str(year),
                "month": str(month),
                "fred_series_id": series_id,
            }
        )
    return rows


def normalize(records: list[dict[str, Any]], msa_code: str, source_id: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in records:
        out.append(
            {
                "metric": "permits_total",
                "value": int(round(float(row["permit"]))),
                "as_of": f"{row['year']}-{int(row['month']):02d}",
                "geo_level": "msa",
                "msa_code": str(msa_code),
                "source_id": source_id,
                "fred_series_id": row.get("fred_series_id"),
            }
        )
    return out


def fetch_annual_county_permits(
    msa_code: str, start_year: int, end_year: int
) -> tuple[list[dict[str, Any]], str | None]:
    """Fetch and sum annual NSA county permits for an MSA without an MSA-level series.

    Returns ``(rows, coverage_note)`` — rows are pre-normalize dicts shaped
    ``{"permit": int, "year": str, "fred_series_ids": "id1,id2,..."}``;
    coverage_note is a human-readable flag for missing counties (or None).

    Raises ``KeyError`` if the MSA isn't in CBSA_TO_FRED_ANNUAL_COUNTIES.
    """
    msa = str(msa_code).strip()
    if msa not in CBSA_TO_FRED_ANNUAL_COUNTIES:
        raise KeyError(msa)
    spec = CBSA_TO_FRED_ANNUAL_COUNTIES[msa]
    series_ids = cast(list[str], spec["series"])
    coverage_note = cast("str | None", spec.get("coverage_note"))

    by_year: dict[str, int] = defaultdict(int)
    for series_id in series_ids:
        response = requests.get(
            FRED_GRAPH_CSV,
            params={"id": series_id},
            headers={"User-Agent": "curl/8.6.0"},
            timeout=30,
        )
        response.raise_for_status()
        reader = csv.DictReader(io.StringIO(response.text))
        for row in reader:
            raw_value = str(row.get(series_id, "") or "").strip()
            if raw_value in {"", "."}:
                continue
            date = str(row.get("observation_date", "")).strip()
            if len(date) < 4:
                continue
            year = int(date[:4])
            if year < start_year or year > end_year:
                continue
            try:
                permit = float(raw_value)
            except ValueError:
                continue
            by_year[str(year)] += int(round(permit))

    rows = [
        {"permit": permits, "year": year, "fred_series_ids": ",".join(series_ids)}
        for year, permits in sorted(by_year.items())
    ]
    return rows, coverage_note


def normalize_annual(
    records: list[dict[str, Any]],
    msa_code: str,
    source_id: str,
    coverage_note: str | None,
) -> list[dict[str, Any]]:
    return [
        {
            "metric": "permits_total_annual_nsa",
            "value": int(row["permit"]),
            "as_of": str(row["year"]),
            "geo_level": "msa",
            "msa_code": str(msa_code),
            "source_id": source_id,
            "fred_series_ids": row["fred_series_ids"],
            "frequency": "annual",
            "adjustment": "NSA",
            "coverage_note": coverage_note,
        }
        for row in records
    ]


def write_output(records: list[dict[str, Any]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if pd is None:
        with path.with_suffix(".jsonl").open("w", encoding="utf-8") as handle:
            for row in records:
                handle.write(json.dumps(row) + "\n")
        return
    assert pd is not None  # guard for type checker
    frame = pd.DataFrame.from_records(records)
    frame.to_parquet(path, index=False)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("msa_code", help="CBSA code, e.g., 19100 for Dallas-Plano-Irving MD.")
    parser.add_argument(
        "--output",
        default=None,
        help=(
            "Output parquet path. Defaults: "
            "data/public/processed/macro/census_permits_<cbsa>.parquet (monthly SA) "
            "or data/public/processed/macro/permits_annual_nsa_<cbsa>.parquet (annual fallback)."
        ),
    )
    parser.add_argument("--start-year", type=int, default=2015)
    parser.add_argument("--end-year", type=int, default=dt.date.today().year)
    parser.add_argument("--source-id", default="CENSUS-BPS-via-FRED")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    msa = str(args.msa_code).strip()

    if msa in CBSA_TO_FRED:
        raw = fetch_permits(msa, args.start_year, args.end_year)
        normalized = normalize(raw, msa, args.source_id)
        out_path = Path(args.output or f"data/public/processed/macro/census_permits_{msa}.parquet")
        write_output(normalized, out_path)
        print(f"[permits] {msa} (monthly SA): wrote {len(normalized)} rows to {out_path}")
        return 0

    if msa in CBSA_TO_FRED_ANNUAL_COUNTIES:
        rows, coverage_note = fetch_annual_county_permits(msa, args.start_year, args.end_year)
        normalized = normalize_annual(rows, msa, args.source_id, coverage_note)
        out_path = Path(
            args.output or f"data/public/processed/macro/permits_annual_nsa_{msa}.parquet"
        )
        write_output(normalized, out_path)
        print(
            f"[permits] {msa} (annual NSA county sum): wrote {len(normalized)} rows to {out_path}"
        )
        if coverage_note:
            print(f"[permits] {msa} coverage note: {coverage_note}", file=sys.stderr)
        return 0

    print(
        f"[permits] No FRED permit series for CBSA {msa}. "
        f"Monthly SA MSAs: {', '.join(sorted(CBSA_TO_FRED))}. "
        f"Annual NSA county-sum MSAs: {', '.join(sorted(CBSA_TO_FRED_ANNUAL_COUNTIES))}. "
        f"Skipping (exit 0).",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
