"""Ingest FRED building-permits series and persist normalized output.

This is intentionally lightweight: it uses the public `fredgraph.csv` export endpoint,
which does not require an API key.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import urllib.parse
import urllib.request
from io import StringIO
from pathlib import Path
from typing import Any, cast

pd: Any | None
try:
    import pandas as _pd
except ImportError:  # pragma: no cover - optional dependency
    pd = None
else:  # pragma: no cover - optional dependency
    pd = cast(Any, _pd)


FRED_GRAPH_CSV = "https://fred.stlouisfed.org/graph/fredgraph.csv"


def fetch_fred_series(series_id: str, timeout_s: int = 30) -> list[dict[str, Any]]:
    params = {"id": series_id}
    url = f"{FRED_GRAPH_CSV}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": "market-study-agent/1.0"})
    with urllib.request.urlopen(req, timeout=timeout_s) as response:
        payload = response.read().decode("utf-8")

    reader = csv.DictReader(StringIO(payload))
    out: list[dict[str, Any]] = []
    for row in reader:
        raw_value = row.get(series_id, ".")
        if raw_value is None:
            continue
        raw_value = str(raw_value).strip()
        if raw_value == "" or raw_value == ".":
            continue
        out.append(
            {
                "series_id": series_id,
                "date": row["observation_date"],
                "value": float(raw_value),
            }
        )
    return out


def write_output(records: list[dict[str, Any]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix.lower() == ".jsonl":
        with path.open("w", encoding="utf-8") as handle:
            for row in records:
                handle.write(json.dumps(row) + "\n")
        return
    if path.suffix.lower() == ".json":
        path.write_text(json.dumps(records, indent=2) + "\n", encoding="utf-8")
        return

    if pd is None:
        with path.with_suffix(".jsonl").open("w", encoding="utf-8") as handle:
            for row in records:
                handle.write(json.dumps(row) + "\n")
        return

    assert pd is not None  # guard for type checker
    frame = pd.DataFrame.from_records(records)
    frame.to_parquet(path, index=False)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch building permits series from FRED.")
    parser.add_argument(
        "--series-id",
        action="append",
        required=True,
        help="FRED series id (repeatable), e.g. DALL148BPPRIVSA.",
    )
    parser.add_argument(
        "--derive-subtract",
        action="append",
        default=[],
        help=(
            "Create a derived metric by subtracting two fetched series: "
            "`metric=LEFT-RIGHT`, e.g. `permits_multi_2plus_sa=DALL148BPPRIVSA-DALL148BP1FHSA`."
        ),
    )
    parser.add_argument(
        "--derive-ratio",
        action="append",
        default=[],
        help=(
            "Create a derived metric by dividing two series: `metric=NUM/DEN`, "
            "e.g. `us_share_5plus_of_2plus=PERMIT5/us_permits_2plus_saar`. "
            "Dates are aligned by intersection."
        ),
    )
    parser.add_argument(
        "--derive-multiply",
        action="append",
        default=[],
        help=(
            "Create a derived metric by multiplying two series: `metric=LEFT*RIGHT`, "
            "e.g. `dfw_permits_5plus_est_sa=permits_multi_2plus_sa*us_share_5plus_of_2plus`."
        ),
    )
    parser.add_argument(
        "--output",
        default="data/public/processed/macro/fred_series.parquet",
        help="Output parquet path (jsonl if pandas unavailable).",
    )
    parser.add_argument("--source-id", default="FRED", help="Source registry id.")
    parser.add_argument(
        "--retrieved-at-utc",
        default=dt.datetime.now(dt.UTC).isoformat(),
        help="UTC timestamp for provenance (defaults to now).",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    series_ids = cast(list[str], args.series_id)
    derive_subtract = cast(list[str], args.derive_subtract)
    derive_ratio = cast(list[str], args.derive_ratio)
    derive_multiply = cast(list[str], args.derive_multiply)

    records: list[dict[str, Any]] = []
    by_series: dict[str, dict[str, float]] = {}
    for series_id in series_ids:
        raw = fetch_fred_series(series_id)
        series_map: dict[str, float] = {}
        for row in raw:
            as_of = str(row["date"])
            value = float(row["value"])
            series_map[as_of] = value
            records.append(
                {
                    "metric": series_id,
                    "value": value,
                    "as_of": as_of,
                    "geo_level": "unknown",
                    "geo_id": None,
                    "fred_series_id": series_id,
                    "source_id": str(args.source_id),
                    "retrieved_at_utc": str(args.retrieved_at_utc),
                }
            )
        by_series[series_id] = series_map

    for spec in derive_subtract:
        if "=" not in spec or "-" not in spec:
            raise ValueError(f"Invalid --derive-subtract format: {spec}")
        metric_name, expr = spec.split("=", 1)
        left_id, right_id = expr.split("-", 1)
        if left_id not in by_series or right_id not in by_series:
            raise ValueError(
                f"Missing series for derived metric {metric_name}: {left_id} or {right_id}"
            )
        left = by_series[left_id]
        right = by_series[right_id]
        subtract_map: dict[str, float] = {}
        for as_of in sorted(set(left).intersection(right)):
            value = float(left[as_of] - right[as_of])
            subtract_map[as_of] = value
            records.append(
                {
                    "metric": metric_name,
                    "value": value,
                    "as_of": as_of,
                    "geo_level": "unknown",
                    "geo_id": None,
                    "fred_series_id": None,
                    "source_id": str(args.source_id),
                    "retrieved_at_utc": str(args.retrieved_at_utc),
                }
            )
        by_series[metric_name] = subtract_map

    for spec in derive_ratio:
        if "=" not in spec or "/" not in spec:
            raise ValueError(f"Invalid --derive-ratio format: {spec}")
        metric_name, expr = spec.split("=", 1)
        num_id, den_id = expr.split("/", 1)
        if num_id not in by_series or den_id not in by_series:
            raise ValueError(
                f"Missing series for derived metric {metric_name}: {num_id} or {den_id}"
            )
        num = by_series[num_id]
        den = by_series[den_id]
        ratio_map: dict[str, float] = {}
        for as_of in sorted(set(num).intersection(den)):
            if den[as_of] == 0:
                continue
            value = float(num[as_of] / den[as_of])
            ratio_map[as_of] = value
            records.append(
                {
                    "metric": metric_name,
                    "value": value,
                    "as_of": as_of,
                    "geo_level": "unknown",
                    "geo_id": None,
                    "fred_series_id": None,
                    "source_id": str(args.source_id),
                    "retrieved_at_utc": str(args.retrieved_at_utc),
                }
            )
        by_series[metric_name] = ratio_map

    for spec in derive_multiply:
        if "=" not in spec or "*" not in spec:
            raise ValueError(f"Invalid --derive-multiply format: {spec}")
        metric_name, expr = spec.split("=", 1)
        left_id, right_id = expr.split("*", 1)
        if left_id not in by_series or right_id not in by_series:
            raise ValueError(
                f"Missing series for derived metric {metric_name}: {left_id} or {right_id}"
            )
        left = by_series[left_id]
        right = by_series[right_id]
        multiply_map: dict[str, float] = {}
        for as_of in sorted(set(left).intersection(right)):
            value = float(left[as_of] * right[as_of])
            multiply_map[as_of] = value
            records.append(
                {
                    "metric": metric_name,
                    "value": value,
                    "as_of": as_of,
                    "geo_level": "unknown",
                    "geo_id": None,
                    "fred_series_id": None,
                    "source_id": str(args.source_id),
                    "retrieved_at_utc": str(args.retrieved_at_utc),
                }
            )
        by_series[metric_name] = multiply_map

    write_output(records, Path(str(args.output)))


if __name__ == "__main__":
    main()
