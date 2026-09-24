from __future__ import annotations

import pytest
from etl.gainesville.sources.property_direct_config import (
    PropertyDirectConfigError,
    PropertyDirectSite,
    parse_sites,
)


def test_parse_sites_minimum_valid() -> None:
    raw = [
        {
            "name": "Tower View",
            "url": "https://www.towerviewgainesville.com/floorplans",
            "selectors": {
                "floorplan_card": ".fp-card",
                "name": ".fp-name",
                "rent": ".fp-rent",
                "beds": ".fp-beds",
                "baths": ".fp-baths",
                "sqft": ".fp-sqft",
            },
            "address": "500 Pine Drive, Gainesville, TX 76240",
        },
    ]
    sites = parse_sites(raw)
    assert len(sites) == 1
    assert isinstance(sites[0], PropertyDirectSite)
    assert sites[0].name == "Tower View"
    assert sites[0].address == "500 Pine Drive, Gainesville, TX 76240"


def test_parse_sites_missing_url_raises() -> None:
    raw = [{"name": "X", "selectors": {}}]
    with pytest.raises(PropertyDirectConfigError, match="url"):
        parse_sites(raw)


def test_parse_sites_missing_selector_raises() -> None:
    raw = [{
        "name": "X",
        "url": "https://x.com",
        "selectors": {"floorplan_card": ".x"},  # missing rent/beds/baths/sqft/name
        "address": "123 X",
    }]
    with pytest.raises(PropertyDirectConfigError, match="selector"):
        parse_sites(raw)


def test_parse_empty_list_returns_empty() -> None:
    assert parse_sites([]) == []


def test_parse_sites_name_filter_is_passed_through() -> None:
    """name_filter present in YAML is carried on the resulting PropertyDirectSite."""
    raw = [
        {
            "name": "Shady Glen Apartments (Klement)",
            "url": "https://klementproperties.com/apartments/",
            "address": "719 S. Weaver, Gainesville, TX 76240",
            "name_filter": "shady glen",
            "selectors": {
                "floorplan_card": "#listing_div",
                "name": "#title",
                "rent": "tr.table-success td:nth-of-type(4)",
                "beds": "tr.table-success td:nth-of-type(2)",
                "baths": "tr.table-success td:nth-of-type(3)",
                "sqft": "",
            },
        }
    ]
    sites = parse_sites(raw)
    assert len(sites) == 1
    assert sites[0].name_filter == "shady glen"


def test_parse_sites_name_filter_absent_defaults_to_none() -> None:
    """Sites without name_filter in YAML should have name_filter=None."""
    raw = [
        {
            "name": "Tower View",
            "url": "https://www.towerviewgainesville.com/floorplans",
            "address": "500 Pine Drive, Gainesville, TX 76240",
            "selectors": {
                "floorplan_card": ".fp-card",
                "name": ".fp-name",
                "rent": ".fp-rent",
                "beds": ".fp-beds",
                "baths": ".fp-baths",
                "sqft": ".fp-sqft",
            },
        }
    ]
    sites = parse_sites(raw)
    assert len(sites) == 1
    assert sites[0].name_filter is None
