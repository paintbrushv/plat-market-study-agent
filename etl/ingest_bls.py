"""Ingest BLS LAUS data for a metro area and persist normalized output."""

from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path
from typing import Any, cast

pd: Any | None
try:
    import pandas as _pd
except ImportError:  # pragma: no cover - optional dependency
    pd = None
else:  # pragma: no cover - optional dependency
    pd = cast(Any, _pd)

BLS_ENDPOINT = "https://api.bls.gov/publicAPI/v2/timeseries/data/"
# Example series IDs from BLS EAG pages look like: LAUMT481910000000003
# where the last 9 digits encode the LAUS measure (e.g., unemployment rate).
SERIES_TEMPLATE = "LAUMT{msa_code}000000003"


def _normalize_msa_code(msa_code: str) -> str:
    """
    BLS LAUS MSA codes in series IDs are 6 digits (e.g., Austin: 481910).
    Accept 5-digit CBSA-style inputs and left-pad (e.g., Dallas-Fort Worth: 19100 -> 019100).
    """
    cleaned = str(msa_code).strip()
    if cleaned.isdigit() and len(cleaned) in {5, 6}:
        return cleaned.zfill(6)
    return cleaned


def fetch_laus_series(msa_code: str, start_year: int, end_year: int) -> list[dict[str, Any]]:
    """Fetch unemployment rate series for an MSA.

    This uses the public BLS API. Authentication keys can be passed via the BLS_API_KEY
    environment variable (handled by urllib automatically if provided in the payload).
    """

    normalized_code = _normalize_msa_code(msa_code)
    payload = {
        "seriesid": [SERIES_TEMPLATE.format(msa_code=normalized_code)],
        "startyear": start_year,
        "endyear": end_year,
    }
    # Lazy import to avoid hard dependency if unused immediately.
    import urllib.request

    req = urllib.request.Request(
        BLS_ENDPOINT,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req) as response:
        raw_payload = response.read().decode("utf-8")

    payload_json = cast(dict[str, Any], json.loads(raw_payload))
    results = cast(dict[str, Any], payload_json["Results"])
    series_list = cast(list[Any], results["series"])
    first_series = cast(dict[str, Any], series_list[0])
    data_points = cast(list[dict[str, Any]], first_series["data"])
    return data_points


def fetch_laus_series_chunked(
    msa_code: str, start_year: int, end_year: int
) -> list[dict[str, Any]]:
    """
    Fetch LAUS data in chunks to avoid API range limits.

    The BLS API can cap the number of years returned per request; chunking makes the
    results deterministic across environments (with/without API keys).
    """
    chunk_size_years = 10
    all_points: list[dict[str, Any]] = []
    for chunk_start in range(int(start_year), int(end_year) + 1, chunk_size_years):
        chunk_end = min(int(end_year), chunk_start + chunk_size_years - 1)
        all_points.extend(fetch_laus_series(msa_code, chunk_start, chunk_end))

    # De-dupe by (year, period) in case of overlaps.
    seen: set[tuple[str, str]] = set()
    deduped: list[dict[str, Any]] = []
    for item in all_points:
        key = (str(item.get("year", "")), str(item.get("period", "")))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def normalize_records(
    raw: list[dict[str, Any]],
    msa_code: str,
    source_id: str,
) -> list[dict[str, Any]]:
    msa_code = _normalize_msa_code(msa_code)
    normalized = []
    for item in raw:
        raw_value = str(item.get("value", "")).strip()
        if raw_value in {"", ".", "-", "NA", "N/A"}:
            continue
        try:
            value = float(raw_value)
        except ValueError:
            continue
        normalized.append(
            {
                "metric": "unemployment_rate",
                "value": value,
                "as_of": f"{item['year']}-{item['periodName']}",
                "geo_level": "msa",
                "msa_code": msa_code,
                "source_id": source_id,
            }
        )
    return normalized


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
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "msa_code", help="BLS LAUS metro code, e.g., 481910 for Dallas-Fort Worth-Arlington."
    )
    parser.add_argument("--output", default="data/public/processed/macro/bls_output.parquet")
    parser.add_argument("--start-year", type=int, default=2015)
    parser.add_argument("--end-year", type=int, default=dt.date.today().year)
    parser.add_argument("--source-id", default="BLS-LAUS")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    raw = fetch_laus_series_chunked(args.msa_code, args.start_year, args.end_year)
    records = normalize_records(raw, args.msa_code, args.source_id)
    write_output(records, Path(args.output))


if __name__ == "__main__":
    main()
