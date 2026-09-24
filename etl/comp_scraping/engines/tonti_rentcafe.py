"""Tonti WordPress proxy for public RentCafe floorplan data.

Tonti property sites expose a same-origin WordPress REST endpoint at
``/wp-json/rentcafeapi/v2/floorplans``. The marketing page hydrates its
floorplan UI from that endpoint, whose field names differ from RentCafe's
embedded JSON and therefore require an explicit adapter.
"""

from __future__ import annotations

import json
from typing import Any

from etl.comp_scraping.engines._base import SimpleEngine, _parse_specials_banner
from etl.comp_scraping.normalize import normalize_shape
from etl.comp_scraping.types import PageSpec, PartialResult


def parse_tonti_rentcafe_floorplans(content: str) -> dict[str, Any]:
    """Convert the Tonti RentCafe WordPress response to canonical Shape B."""

    try:
        payload = json.loads(content)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ValueError("Invalid Tonti RentCafe floorplan response") from exc

    floorplans = payload.get("floorplans") if isinstance(payload, dict) else None
    if not isinstance(floorplans, list):
        raise ValueError("Tonti RentCafe response has no floorplans list")

    parsed_floorplans: list[dict[str, Any]] = []
    for floorplan in floorplans:
        if not isinstance(floorplan, dict):
            continue
        try:
            rent_min = float(floorplan.get("MinimumRent") or 0)
            rent_max = float(floorplan.get("MaximumRent") or rent_min)
            beds = int(float(floorplan.get("Beds") or 0))
            baths = float(floorplan.get("Baths") or 1)
            sqft = float(
                floorplan.get("MinimumSQFT")
                or floorplan.get("MaximumSQFT")
                or 0
            )
            available_count = int(floorplan.get("AvailableUnitsCount") or 0)
        except (TypeError, ValueError):
            continue
        if rent_min <= 0:
            continue
        parsed_floorplans.append(
            {
                "floorplan_name": str(floorplan.get("FloorplanName") or "").strip(),
                "beds": beds,
                "baths": baths,
                "sqft": sqft,
                "rent_min": rent_min,
                "rent_max": rent_max,
                "available_units": available_count,
            }
        )

    if not parsed_floorplans:
        raise ValueError("Tonti RentCafe response has no priced floorplans")

    return {
        "platform": "tonti_rentcafe",
        "floorplans": parsed_floorplans,
        "units": [],
        "specials": [],
    }


class _TontiRentCafeEngine(SimpleEngine):
    def parse_page(self, spec: PageSpec, content: Any) -> PartialResult:  # noqa: ANN401
        text = content if isinstance(content, str) else ""
        if spec.parser == "specials_banner":
            return _parse_specials_banner(spec.name, text)
        try:
            payload = parse_tonti_rentcafe_floorplans(text)
        except ValueError:
            return PartialResult(page_name=spec.name)
        normalised = normalize_shape(payload)
        return PartialResult(
            page_name=spec.name,
            floorplans=list(normalised.get("floorplans") or []),
            units=list(normalised.get("units") or []),
            specials=list(normalised.get("specials") or []),
            raw_meta={"parser": "tonti_rentcafe"},
        )


TONTI_RENTCAFE_ENGINE = _TontiRentCafeEngine(
    name="tonti_rentcafe",
    url_substrings=[
        "thelexingtonatvalleyranch.com",
        "highlandsofvalleyranch.com",
    ],
    html_fingerprints=[
        "wp-json/rentcafeapi/v2/floorplans",
        "rentcafeapi/v2/apartmentavailability-get-multiple-plans",
    ],
    parser=parse_tonti_rentcafe_floorplans,
    pages=[
        PageSpec(
            name="floorplans_api",
            url_template="{base}/wp-json/rentcafeapi/v2/floorplans",
            fetch_method="auto",
            parser="floorplans_api",
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
