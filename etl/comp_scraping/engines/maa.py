"""MAA Next.js property pages."""

from __future__ import annotations

import re
from typing import Any

from etl.comp_scraping.engines._base import SimpleEngine
from etl.comp_scraping.types import PageSpec


def _strip_tags(html_text: str) -> str:
    text = re.sub(r"<script[\s\S]*?</script>", " ", html_text, flags=re.IGNORECASE)
    text = re.sub(r"<style[\s\S]*?</style>", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " | ", text)
    return re.sub(r"\s+", " ", text).strip()


def parse_maa_available_units(html_text: str) -> dict[str, Any]:
    """Parse rendered MAA available-unit cards.

    MAA pages render the available-unit carousel into the public DOM. The card
    text carries unit number, beds/baths, square feet, move-in window, and
    starting rent.
    """

    text = _strip_tags(html_text)
    if "MAA" not in text or "Rent starting at" not in text:
        raise ValueError("No rendered MAA available-unit cards found")

    pattern = re.compile(
        r"Unit\s+#(?P<unit>[A-Za-z0-9-]+)[\s|]*"
        r"(?P<beds>\d+)\s*Beds?,\s*(?P<baths>\d+(?:\.\d+)?)\s*Baths?[\s|]*"
        r"(?P<sqft>[\d,]+)\s*Sq\.\s*Ft\.[\s|]*"
        r"(?P<body>[\s\S]{0,320}?)Rent starting at[\s|]*\$[\s|]*(?P<rent>[\d,]+)"
        r"(?P<tail>[\s\S]{0,220}?)(?=Unit\s+#|$)",
        re.IGNORECASE,
    )
    unit_rows: list[dict[str, Any]] = []
    for match in pattern.finditer(text):
        plan_text = f"{match.group('body')} {match.group('tail')}"
        plan_match = re.search(
            r"(?:\|\s*)?(?:Pool View\s*)?(?P<plan>The\s+[A-Za-z][A-Za-z ]+?)\s+"
            r"(?P<plan_sqft>[\d,]+)\s*SF",
            plan_text,
            re.IGNORECASE,
        )
        move_match = re.search(r"Move-in:\s*\|\s*([^|]+(?:\|\s*[^|]+)?)", plan_text)
        unit_rows.append(
            {
                "unit_number": match.group("unit"),
                "beds": int(match.group("beds")),
                "baths": float(match.group("baths")),
                "sqft": float(match.group("sqft").replace(",", "")),
                "rent": float(match.group("rent").replace(",", "")),
                "available_date": re.sub(r"\s*\|\s*", " ", move_match.group(1)).strip()
                if move_match
                else None,
                "available_count": 1,
                "apply_url": None,
                "floorplan_id": None,
                "floorplan_name": plan_match.group("plan").strip() if plan_match else None,
            }
        )

    if not unit_rows:
        raise ValueError("MAA page rendered but no priced unit rows extracted")

    floorplans_by_key: dict[tuple[Any, ...], dict[str, Any]] = {}
    for row in unit_rows:
        key = (
            row.get("floorplan_name") or f"{int(row['beds'])}BR/{row['baths']}BA",
            row["beds"],
            row["baths"],
            row["sqft"],
        )
        current = floorplans_by_key.setdefault(
            key,
            {
                "floorplan_name": key[0],
                "beds": row["beds"],
                "baths": row["baths"],
                "sqft": row["sqft"],
                "rent_min": row["rent"],
                "rent_max": row["rent"],
                "available_units": 0,
            },
        )
        current["rent_min"] = min(float(current["rent_min"]), float(row["rent"]))
        current["rent_max"] = max(float(current["rent_max"]), float(row["rent"]))
        current["available_units"] = int(current["available_units"]) + 1

    return {
        "platform": "maa_rendered_units",
        "floorplans": list(floorplans_by_key.values()),
        "units": unit_rows,
        "specials": [],
    }


MAA_ENGINE = SimpleEngine(
    name="maa",
    url_substrings=["maac.com/"],
    html_fingerprints=["property-available-apartments", "Rent starting at"],
    parser=parse_maa_available_units,
    pages=[
        PageSpec(
            name="property_page",
            url_template="{base}/",
            fetch_method="playwright",
            parser="floorplans",
            required=True,
        ),
    ],
)
