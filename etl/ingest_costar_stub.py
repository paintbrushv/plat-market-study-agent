"""Placeholder for CoStar ingestion.

This script intentionally avoids implementing direct access to paid datasets. Instead it
illustrates how a wrapper might read pre-exported aggregates stored in `data/paid/` and
convert them into the canonical schema.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path
from typing import Any, cast


def load_paid_extract(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    return cast(list[dict[str, Any]], data)


def normalize(records: list[dict[str, Any]], source_id: str) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for row in records:
        normalized.append(
            {
                "metric": row["metric"],
                "value": row["value"],
                "as_of": row.get("as_of") or dt.date.today().isoformat(),
                "geo_level": row.get("geo_level", "msa"),
                "msa_code": row.get("msa_code"),
                "source_id": source_id,
                "license": "restricted",
            }
        )
    return normalized


def write_output(records: list[dict[str, Any]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in records:
            handle.write(json.dumps(row) + "\n")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("input", help="Path to a gated JSON aggregate exported from CoStar.")
    parser.add_argument(
        "--output",
        default="data/paid/processed/costar_normalized.jsonl",
        help="Destination for normalized aggregates.",
    )
    parser.add_argument("--source-id", default="COSTAR-MKT")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    records = load_paid_extract(Path(args.input))
    normalized = normalize(records, args.source_id)
    write_output(normalized, Path(args.output))


if __name__ == "__main__":
    main()
