from __future__ import annotations

from etl.comp_scraping.engines._rentcafe_card import (
    parse_rentcafe_action_button_floorplans,
)


def test_rentcafe_action_button_skips_unpriced_duplicate_before_apply_link() -> None:
    html = """
    <a data-floorplan-name="A1" data-floorplan-size="1"
       data-floorplan-bath="1" data-floorplan-sqft="788"
       data-floorplan-price="">
       Schedule a Tour
    </a>
    <a data-floorplan-name="A1" data-floorplan-size="1"
       data-floorplan-bath="1" data-floorplan-sqft="788"
       data-floorplan-price="$894 - $1,117"
       data-floorplan-availability="6">
       Apply Now
    </a>
    """

    out = parse_rentcafe_action_button_floorplans(html)

    assert len(out["floorplans"]) == 1
    plan = out["floorplans"][0]
    assert plan["floorplan_name"] == "A1"
    assert plan["rent_min"] == 894
    assert plan["rent_max"] == 1117
    assert plan["available_units"] == 6
