from __future__ import annotations

from etl.comp_scraping.engines.resi_rendered import RESI_RENDERED_ENGINE
from etl.comp_scraping.types import PageSpec


def test_resi_rendered_engine_parses_sightmap_inventory_payload() -> None:
    payload = {
        "data": {
            "floor_plans": [
                {
                    "id": "476306",
                    "name": "B3",
                    "bedroom_count": 2,
                    "bathroom_count": 2,
                }
            ],
            "units": [
                {
                    "unit_number": "401",
                    "floor_plan_id": "476306",
                    "area": 983,
                    "price": 1739,
                    "available_on": "2026-05-15",
                },
                {
                    "unit_number": "404",
                    "floor_plan_id": "476306",
                    "area": 983,
                    "price": 1739,
                    "available_on": "2026-07-16",
                },
            ],
        }
    }

    partial = RESI_RENDERED_ENGINE.parse_page(
        PageSpec(
            name="sightmap_inventory",
            url_template="{base}/floor-plans/",
            fetch_method="playwright_xhr",
            parser="sightmap_inventory",
        ),
        payload,
    )

    assert len(partial.units) == 2
    assert partial.units[0]["unit_number"] == "401"
    assert partial.floorplans[0]["floorplan_name"] == "B3"
    assert partial.floorplans[0]["available_units"] == 2
