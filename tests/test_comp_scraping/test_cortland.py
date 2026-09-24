from __future__ import annotations

from pathlib import Path

import pytest
from etl.comp_scraping.engines.cortland import parse_cortland_floorplans

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "example_cortland_floorplans.html"


@pytest.mark.skipif(not FIXTURE.exists(), reason="fixture not captured yet")
def test_cortland_parser_extracts_units() -> None:
    html = FIXTURE.read_text(encoding="utf-8")
    out = parse_cortland_floorplans(html)
    assert out["platform"] == "cortland"
    assert len(out["floorplans"]) >= 4, "expected several distinct floorplans"
    assert all(p.get("rent_min", 0) > 0 for p in out["floorplans"])
    # Pricing sanity
    for p in out["floorplans"]:
        assert 800 < p["rent_min"] < 5000, p
        assert p["beds"] in (0, 1, 2, 3, 4)
        assert p["sqft"] > 0
        assert p["available_units"] >= 1


def test_cortland_parser_raises_without_fingerprint() -> None:
    with pytest.raises(ValueError):
        parse_cortland_floorplans("<html><body>nothing here</body></html>")


def test_cortland_parser_handles_braces_inside_string_values() -> None:
    """An ``amenities`` string containing literal ``{`` or ``}`` must not
    confuse the JSON decoder. Regression test for the prior bracket counter."""

    # Embedded braces inside the amenities string would have made the
    # legacy bracket-counter prematurely close the outer object.
    html = (
        '<script>'
        '{"id":1,"apartment_number":"A101","floorplan":3,'
        '"floorplan_name":"A1","bedrooms":"1","bathrooms":"1.0",'
        '"rent_min":1500,"rent_max":1600,"square_feet":700,'
        '"amenities":"Gym {open 24h} and pool {heated}"}'
        ',{"id":2,"apartment_number":"A102","floorplan":3,'
        '"floorplan_name":"A1","bedrooms":"1","bathrooms":"1.0",'
        '"rent_min":1550,"rent_max":1650,"square_feet":700,'
        '"amenities":null}'
        "</script>"
    )
    out = parse_cortland_floorplans(html)
    assert len(out["floorplans"]) == 1
    plan = out["floorplans"][0]
    assert plan["available_units"] == 2
    assert plan["rent_min"] == 1500
    assert plan["rent_max"] == 1650
