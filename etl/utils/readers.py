"""Utility helpers for reading raw and processed data files."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

pd: Any | None
try:
    import pandas as _pd
except ImportError:  # pragma: no cover
    pd = None
else:  # pragma: no cover
    pd = cast(Any, _pd)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def read_parquet(path: Path) -> list[dict[str, Any]]:
    if pd is None:
        raise RuntimeError("pandas is required to read parquet files")
    assert pd is not None
    frame = pd.read_parquet(path)
    records = cast(list[dict[str, Any]], frame.to_dict(orient="records"))
    return records


def read_any(path: Path) -> list[dict[str, Any]]:
    if path.suffix == ".jsonl":
        return read_jsonl(path)
    if path.suffix == ".parquet":
        return read_parquet(path)
    raise ValueError(f"Unsupported file type: {path.suffix}")
