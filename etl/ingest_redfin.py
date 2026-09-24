"""Stub ingestion for Redfin public datasets."""

from __future__ import annotations

import argparse
import csv
import urllib.request
from io import StringIO
from pathlib import Path
from typing import Any, cast

pd: Any | None
try:
    import pandas as _pd
except ImportError:  # pragma: no cover
    pd = None
else:  # pragma: no cover
    pd = cast(Any, _pd)

REDFIN_BASE = "https://redfin-public-data.s3-us-west-2.amazonaws.com"
REDFIN_FILE = "market-trends/metro_market_trends.csv"


def fetch_redfin() -> list[dict[str, Any]]:
    url = f"{REDFIN_BASE}/{REDFIN_FILE}"
    with urllib.request.urlopen(url) as response:
        csv_text = response.read().decode("utf-8")
    reader = csv.DictReader(StringIO(csv_text))
    return list(reader)


def filter_metro(records: list[dict[str, Any]], metro: str) -> list[dict[str, Any]]:
    return [row for row in records if row.get("region") == metro]


def normalize(records: list[dict[str, Any]], source_id: str) -> list[dict[str, Any]]:
    normalized = []
    for row in records:
        normalized.append(
            {
                "metric": "median_sale_price",
                "value": float(row.get("median_sale_price", 0) or 0),
                "as_of": row.get("period_end"),
                "geo_level": "msa",
                "msa_code": row.get("region_code"),
                "source_id": source_id,
            }
        )
    return normalized


def write_output(records: list[dict[str, Any]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if pd is None:
        import json

        with path.with_suffix(".jsonl").open("w", encoding="utf-8") as handle:
            for row in records:
                handle.write(json.dumps(row) + "\n")
        return
    assert pd is not None  # guard for type checker
    pd.DataFrame.from_records(records).to_parquet(path, index=False)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("metro", default="Austin, TX")
    parser.add_argument("--output", default="data/public/processed/metro/redfin_{metro}.parquet")
    parser.add_argument("--source-id", default="REDFIN-PUBLIC")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    raw = fetch_redfin()
    metro_records = filter_metro(raw, args.metro)
    normalized = normalize(metro_records, args.source_id)
    metro_slug = args.metro.lower().replace(",", "").replace(" ", "_")
    output_path = Path(args.output.format(metro=metro_slug))
    write_output(normalized, output_path)


if __name__ == "__main__":
    main()
