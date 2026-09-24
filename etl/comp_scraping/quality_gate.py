"""Quality gate for engine output.

A scrape that returns zero usable floorplans should not be silently treated
as success — that's how a regressed parser leaks into the snapshot. The
gate applies a per-comp minimum-floorplan threshold (scaled to the property's
known unit count) and rejects all-zero-rent payloads.
"""

from __future__ import annotations

from typing import Any


def min_threshold(units: int | None) -> int:
    """Minimum floorplans we expect for a property with ``units`` total units.

    Heuristic: roughly one plan per 20 units, floor 1, ceiling 8. A 27-unit
    boutique requires 1; a 273-unit Class A requires 8.
    """

    if not units or units <= 0:
        return 1
    return max(1, min(8, units // 20))


def passes(payload: dict[str, Any], *, min_plans: int) -> bool:
    """Return True when the payload looks like a usable Shape-B result."""

    if not isinstance(payload, dict):
        return False

    plans = payload.get("floorplans") or []
    if not isinstance(plans, list) or len(plans) < min_plans:
        return False

    priced_count = sum(
        1
        for p in plans
        if isinstance(p, dict)
        and (float(p.get("rent_min") or 0) > 0 or float(p.get("rent_max") or 0) > 0)
    )
    return priced_count >= 2
