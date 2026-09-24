"""LiveBH (BH Management) WordPress engine.

The listing page (e.g.
``livebh.com/apartments/<property>/floor-plans/``) embeds the full per-floorplan
dataset as an HTML-entity-encoded JSON array on a ``data-floorplans=`[...]```
attribute — no need to fetch detail pages. Each record carries
``floorplan_name``, ``floorplan_bedrooms``, ``floorplan_bathrooms``,
``floorplan_rent_min/max``, ``floorplan_sqft_min/max``,
``floorplan_units_available``, and ``floorplan_post_link``.
"""

from __future__ import annotations

import html as _html
import json
import re
from typing import Any

from etl.comp_scraping.engines._base import SimpleEngine
from etl.comp_scraping.types import PageSpec


def _to_int(value: Any) -> int:  # noqa: ANN401
    try:
        return int(float(str(value or 0)))
    except (TypeError, ValueError):
        return 0


def _to_float(value: Any) -> float:  # noqa: ANN401
    try:
        return float(str(value or 0))
    except (TypeError, ValueError):
        return 0.0


def parse_livebh_floorplans(html_text: str) -> dict[str, Any]:
    """Extract the inline ``data-floorplans`` JSON array and map to Shape A."""

    m = re.search(r"data-floorplans='(\[.*?\])'", html_text, re.DOTALL)
    if not m:
        raise ValueError("LiveBH data-floorplans attribute not found")

    try:
        records = json.loads(_html.unescape(m.group(1)))
    except json.JSONDecodeError as exc:
        raise ValueError(f"LiveBH JSON parse failed: {exc}") from exc

    if not isinstance(records, list) or not records:
        raise ValueError("LiveBH JSON empty")

    unit_rows: list[dict[str, Any]] = []
    for fp in records:
        if not isinstance(fp, dict):
            continue
        beds = _to_int(fp.get("floorplan_bedrooms"))
        baths = _to_float(fp.get("floorplan_bathrooms"))
        sqft = _to_float(fp.get("floorplan_sqft_min"))
        rent_min = _to_float(fp.get("floorplan_rent_min"))
        rent_max = _to_float(fp.get("floorplan_rent_max")) or rent_min
        avail = _to_int(fp.get("floorplan_units_available"))
        name = str(fp.get("floorplan_name") or fp.get("floorplan_post_title") or "").strip()
        if not name:
            name = f"{beds}BR/{baths:.0f}BA"
        if rent_min <= 0 and sqft <= 0:
            continue
        unit_rows.append(
            {
                "unit_number": None,
                "beds": beds,
                "baths": baths,
                "sqft": sqft,
                "rent": rent_min,
                "rent_max": rent_max,
                "available_date": None,
                "available_count": avail,
                "apply_url": str(fp.get("floorplan_post_link") or "") or None,
                "floorplan_id": str(fp.get("floorplan_post_id") or "") or None,
                "floorplan_name": name,
            }
        )

    if not unit_rows:
        raise ValueError("LiveBH JSON parsed but no usable rows")

    specials: list[str] = []
    for pat in (r"(\d+\s*weeks?\s*free[^<.]{0,120})", r"(\d+\s*months?\s*free[^<.]{0,120})"):
        for sm in re.finditer(pat, html_text, re.IGNORECASE):
            snippet = re.sub(r"\s+", " ", sm.group(1)).strip()
            if snippet not in specials:
                specials.append(snippet)

    return {
        "available_units": unit_rows,
        "floorplan_summary": [],
        "specials": specials[:5],
        "parser": "livebh",
    }


LIVEBH_ENGINE = SimpleEngine(
    name="livebh",
    url_substrings=["livebh.com"],
    html_fingerprints=["data-floorplans='[", "rentpress_post_templates_floorplan"],
    parser=parse_livebh_floorplans,
    pages=[
        PageSpec(
            name="floorplans",
            url_template="{base}/floor-plans/",
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
    ],
)
