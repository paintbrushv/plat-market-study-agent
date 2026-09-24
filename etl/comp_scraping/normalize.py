"""Shape A → Shape B normalisation.

Existing parsers in :mod:`etl.collect_comps_snapshot` return one of two shapes:

* **Shape A** (``parse_entrata``, ``parse_rentcafe``, ``parse_sightmap``,
  ``parse_swifty``, ``parse_jonah``, ``parse_h2_realestate_listings``,
  ``parse_appfolio_listings_page``)::

      {"available_units": [...], "floorplan_summary": [], "specials": [...],
       "parser": "<name>"}

* **Shape B** (``parse_resi_floorplans_and_units``, ``parse_g5_floorplans_plus``,
  Bryant + AppFolio hybrid)::

      {"platform": "<name>", "floorplans": [...], "units": [...],
       "specials": [...]}

``build_report`` in :mod:`etl.collect_comps_snapshot` reads
``direct.get("floorplans")`` to render the markdown table, so Shape A parsers
never populate the report. ``normalize_shape`` rolls Shape A into Shape B by
bucketing ``available_units`` on ``(beds, baths, sqft)``.
"""

from __future__ import annotations

from typing import Any


def summarize_units_by_signature(units: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Bucket per-unit rows into per-floorplan summaries.

    Mirrors the helper of the same name in :mod:`etl.collect_comps_snapshot`.
    Lifted here so engine modules can normalize Shape A output without a
    circular import.
    """

    buckets: dict[tuple[int, float, float], list[dict[str, Any]]] = {}
    for unit in units:
        beds = int(unit.get("beds") or 0)
        baths = float(unit.get("baths") or 0)
        sqft = float(unit.get("sqft") or 0)
        buckets.setdefault((beds, baths, sqft), []).append(unit)

    out: list[dict[str, Any]] = []
    for (beds, baths, sqft), group in sorted(buckets.items(), key=lambda kv: kv[0]):
        rents = [float(u.get("rent") or 0) for u in group if (u.get("rent") or 0) > 0]
        rent_min = min(rents) if rents else 0.0
        rent_max = max(rents) if rents else 0.0
        names = sorted(
            {str(u.get("floorplan_name") or "") for u in group if u.get("floorplan_name")}
        )
        floorplan_name = (
            names[0] if len(names) == 1 else f"{beds}BR/{int(baths)}BA {sqft:.0f} SF"
        )
        out.append(
            {
                "floorplan_name": floorplan_name,
                "beds": beds,
                "baths": baths,
                "sqft": sqft,
                "rent_min": rent_min,
                "rent_max": rent_max,
                "available_units": len(group),
            }
        )
    return out


def normalize_shape(payload: dict[str, Any]) -> dict[str, Any]:
    """Return a Shape B dict regardless of the input shape.

    Pass-through for Shape B inputs (recognised by the ``floorplans`` key).
    For Shape A inputs (``available_units`` key), summarise into per-plan
    rows via ``summarize_units_by_signature``.
    """

    if not isinstance(payload, dict):
        return {"platform": "unknown", "floorplans": [], "units": [], "specials": []}

    floorplans = payload.get("floorplans")
    units_a = payload.get("available_units")

    if isinstance(floorplans, list):
        return {
            "platform": payload.get("platform") or payload.get("parser") or "unknown",
            "floorplans": list(floorplans),
            "units": list(payload.get("units") or units_a or []),
            "specials": list(payload.get("specials") or []),
        }

    if isinstance(units_a, list):
        return {
            "platform": payload.get("parser") or "unknown",
            "floorplans": summarize_units_by_signature(units_a),
            "units": list(units_a),
            "specials": list(payload.get("specials") or []),
        }

    return {"platform": "unknown", "floorplans": [], "units": [], "specials": []}


def merge_partials_default(
    platform: str,
    partials: list[dict[str, Any]],
    *,
    floorplan_dedupe_key: str = "floorplan_name",
) -> dict[str, Any]:
    """Merge per-page partial dicts (already normalised) into one Shape B dict.

    Floorplans are merged across partials by ``floorplan_dedupe_key``: when the
    same plan appears more than once, prefer the higher ``available_units``
    count and the wider rent range. Units and specials accumulate with
    deduplication.
    """

    fp_index: dict[Any, dict[str, Any]] = {}
    fp_order: list[Any] = []
    units: list[dict[str, Any]] = []
    specials: list[str] = []
    sp_seen: set[str] = set()

    for partial in partials:
        for fp in partial.get("floorplans") or []:
            # Compose a key that's specific enough to keep size-distinct plans
            # apart even when they share a name (e.g. several "Studio" plans
            # at different square footages on the same RentCafe page).
            name = fp.get(floorplan_dedupe_key)
            sqft_key = round(float(fp.get("sqft") or 0))
            beds_key = int(fp.get("beds") or 0)
            key = (name, beds_key, sqft_key) if name else (
                fp.get("beds"),
                fp.get("baths"),
                fp.get("sqft"),
                fp.get("rent_min"),
            )
            if key not in fp_index:
                fp_index[key] = dict(fp)
                fp_order.append(key)
            else:
                existing = fp_index[key]
                existing["available_units"] = max(
                    int(existing.get("available_units") or 0),
                    int(fp.get("available_units") or 0),
                )
                rent_min_new = float(fp.get("rent_min") or 0)
                rent_min_existing = float(existing.get("rent_min") or 0)
                if rent_min_new > 0 and (
                    rent_min_existing == 0 or rent_min_new < rent_min_existing
                ):
                    existing["rent_min"] = rent_min_new
                rent_max_new = float(fp.get("rent_max") or 0)
                rent_max_existing = float(existing.get("rent_max") or 0)
                if rent_max_new > rent_max_existing:
                    existing["rent_max"] = rent_max_new
                # Backfill any missing scalar (sqft, beds, baths) from the
                # later partial — useful when one page has only rents.
                for field_name in ("sqft", "beds", "baths"):
                    if not existing.get(field_name) and fp.get(field_name):
                        existing[field_name] = fp[field_name]
        units.extend(partial.get("units") or [])
        for s in partial.get("specials") or []:
            s_clean = str(s).strip()
            if not s_clean or s_clean in sp_seen:
                continue
            sp_seen.add(s_clean)
            specials.append(s_clean)

    return {
        "platform": platform,
        "floorplans": [fp_index[k] for k in fp_order],
        "units": units,
        "specials": specials,
    }
