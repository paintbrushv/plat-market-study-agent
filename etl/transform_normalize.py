"""Normalize various inputs to the canonical metrics schema."""

from __future__ import annotations

import argparse
import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any, cast

from etl.utils.readers import read_any
from etl.utils.validators import build_report, write_report

pd: Any | None
try:
    import pandas as _pd
except ImportError:  # pragma: no cover
    pd = None
else:  # pragma: no cover
    pd = cast(Any, _pd)

CANONICAL_FIELDS = ["metric", "value", "as_of", "geo_level", "msa_code", "source_id"]


def coerce_record(record: dict[str, Any]) -> dict[str, Any]:
    normalized: dict[str, Any] = {}
    for field in CANONICAL_FIELDS:
        normalized[field] = record.get(field)
    return normalized


def load_records(paths: Iterable[Path]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for path in paths:
        for record in read_any(path):
            records.append(coerce_record(record))
    return records


def write_output(records: list[dict[str, Any]], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    if pd is None:
        with output.with_suffix(".jsonl").open("w", encoding="utf-8") as handle:
            for row in records:
                handle.write(json.dumps(row) + "\n")
        return
    assert pd is not None  # guard for type checker
    frame = pd.DataFrame.from_records(records)
    frame.to_parquet(output, index=False)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--inputs",
        nargs="+",
        required=True,
        help="List of files to normalize (jsonl or parquet).",
    )
    parser.add_argument(
        "--output",
        default="data/public/processed/metro/normalized_metrics.parquet",
        help="Destination for normalized metrics.",
    )
    parser.add_argument(
        "--report",
        default="reports/logs/validation_report.json",
        help="Where to write the validation report.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    input_paths = [Path(path) for path in args.inputs]
    records = load_records(input_paths)
    write_output(records, Path(args.output))

    report = build_report(records)
    write_report(report, args.report)

    if not report.passed:
        raise SystemExit("Normalization failed validation. See report for details.")


if __name__ == "__main__":
    main()
