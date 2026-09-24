"""Cortland leasing engine (cortland.com SSR).

Cortland's property pages are server-rendered HTML with embedded per-unit
JSON dicts at byte offsets >1.5MB. Each available unit appears as a JSON
object beginning ``{"id":NNN,"apartment_number":"...","floorplan":NNN,...}``
with fields ``floorplan_name``, ``bedrooms``, ``rent_min``, ``rent_max``,
``square_feet``, plus availability metadata.

Extraction uses :class:`json.JSONDecoder` so JSON-string fields containing
``{`` or ``}`` (e.g. an amenity description with ``"{heated pool}"``) parse
correctly without a hand-rolled bracket counter.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from typing import Any

from etl.comp_scraping._coerce import coerce_float, coerce_int
from etl.comp_scraping.engines._base import SimpleEngine
from etl.comp_scraping.types import PageSpec

_UNIT_OPEN = re.compile(r'\{"id":\d+,"apartment_number":"[^"]+"')
_DECODER = json.JSONDecoder()


def parse_cortland_floorplans(html_text: str) -> dict[str, Any]:
    """Parse cortland.com floorplan pages.

    Raises ``ValueError`` when no Cortland unit dicts are found (so the
    dispatcher's quality gate / LLM fallback can engage).
    """

    if "rent_min" not in html_text or "floorplan_name" not in html_text:
        raise ValueError("No Cortland unit structure found")

    units_raw: list[dict[str, Any]] = []
    for match in _UNIT_OPEN.finditer(html_text):
        try:
            obj, _end = _DECODER.raw_decode(html_text, match.start())
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(obj, dict) and "rent_min" in obj and "floorplan_name" in obj:
            units_raw.append(obj)

    if not units_raw:
        raise ValueError("Cortland HTML matched fingerprint but no units parsed")

    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for u in units_raw:
        buckets[str(u.get("floorplan_name") or "")].append(u)

    floorplans: list[dict[str, Any]] = []
    units_out: list[dict[str, Any]] = []
    for name, group in sorted(buckets.items()):
        first = group[0]
        beds = coerce_int(first.get("bedrooms"))
        baths = coerce_float(first.get("bathrooms"), default=1.0)
        sqft = coerce_float(first.get("square_feet"))
        rents_min = [
            float(g.get("rent_min") or 0)
            for g in group
            if (g.get("rent_min") or 0) > 0
        ]
        rents_max = [
            float(g.get("rent_max") or 0)
            for g in group
            if (g.get("rent_max") or 0) > 0
        ]
        floorplans.append(
            {
                "floorplan_name": name,
                "beds": beds,
                "baths": baths,
                "sqft": sqft,
                "rent_min": min(rents_min) if rents_min else 0.0,
                "rent_max": max(rents_max) if rents_max else (max(rents_min) if rents_min else 0.0),
                "available_units": len(group),
            }
        )
        for u in group:
            units_out.append(
                {
                    "unit_number": str(u.get("apartment_number") or ""),
                    "beds": beds,
                    "baths": baths,
                    "sqft": sqft,
                    "rent": float(u.get("rent_min") or 0),
                    "rent_max": float(u.get("rent_max") or 0) or None,
                    "available_date": u.get("made_ready_date"),
                    "available_count": 1,
                    "apply_url": None,
                    "floorplan_id": u.get("floorplan"),
                    "floorplan_name": name,
                    "floor": u.get("floor"),
                    "building": u.get("building"),
                }
            )

    return {
        "platform": "cortland",
        "floorplans": floorplans,
        "units": units_out,
        "specials": [],
    }


CORTLAND_ENGINE = SimpleEngine(
    name="cortland",
    url_substrings=["cortland.com/apartments/"],
    html_fingerprints=['"floorplan_name":"', "cortland.com"],
    parser=parse_cortland_floorplans,
    pages=[
        PageSpec(
            name="floorplans",
            url_template="{base}/floorplans/",
            fetch_method="auto",
            parser="floorplans",
            required=True,
        ),
        PageSpec(
            name="home_specials",
            url_template="{base}/",
            fetch_method="auto",
            parser="specials_banner",
        ),
        PageSpec(
            name="apply_specials",
            url_template="{base}/apply/",
            fetch_method="auto",
            parser="specials_banner",
        ),
    ],
)
