"""Lossless numeric coercion helpers shared by engines / extractors.

Centralising these prevents drift between parsers — every engine that pulls
a rent-or-sqft field from messy upstream JSON must apply the same
``"" → 0`` and ``"$1,500" → 1500`` semantics.
"""

from __future__ import annotations

from typing import Any


def coerce_int(value: Any, *, default: int = 0) -> int:  # noqa: ANN401
    if value is None:
        return default
    try:
        if isinstance(value, str):
            cleaned = value.replace(",", "").replace("$", "").strip()
            if not cleaned:
                return default
            return int(float(cleaned))
        return int(float(value))
    except (TypeError, ValueError):
        return default


def coerce_float(value: Any, *, default: float = 0.0) -> float:  # noqa: ANN401
    if value is None:
        return default
    try:
        if isinstance(value, str):
            cleaned = value.replace(",", "").replace("$", "").strip()
            if not cleaned:
                return default
            return float(cleaned)
        return float(value)
    except (TypeError, ValueError):
        return default
