from __future__ import annotations

from pathlib import Path

from etl.gainesville.sources.property_direct import collect_from_html
from etl.gainesville.sources.property_direct_config import PropertyDirectSite

FIXTURES = Path(__file__).parent / "fixtures"


MULTI_PROPERTY_SELECTORS = {
    "floorplan_card": ".fp-card",
    "name": ".fp-name",
    "rent": ".fp-rent",
    "beds": ".fp-beds",
    "baths": ".fp-baths",
    "sqft": ".fp-sqft",
}


def test_collect_from_html_extracts_floorplans() -> None:
    site = PropertyDirectSite(
        name="Tower View",
        url="https://example.com/fp",
        address="500 Pine Drive, Gainesville, TX 76240",
        selectors={
            "floorplan_card": ".fp-card",
            "name": ".fp-name",
            "rent": ".fp-rent",
            "beds": ".fp-beds",
            "baths": ".fp-baths",
            "sqft": ".fp-sqft",
        },
    )
    html = (FIXTURES / "property_direct_floorplans.html").read_text(encoding="utf-8")
    result = collect_from_html(site, html, run_id="r1")
    assert result.status.value == "ok"
    assert len(result.observations) == 2
    obs = result.observations[0]
    assert obs.beds == 1.0
    assert obs.baths == 1.0
    assert obs.sqft == 650
    assert obs.asking_rent == 1250
    assert obs.title == "The Birch — 1BR/1BA"
    assert obs.address_raw == "500 Pine Drive, Gainesville, TX 76240"
    assert obs.listing_kind == "mf"


def test_name_filter_emits_only_matching_card() -> None:
    """With name_filter='shady glen', only 1 of 3 cards should be emitted."""
    site = PropertyDirectSite(
        name="Shady Glen Apartments (Klement)",
        url="https://klementproperties.com/apartments/",
        address="719 S. Weaver, Gainesville, TX 76240",
        selectors=MULTI_PROPERTY_SELECTORS,
        name_filter="shady glen",
    )
    html = (FIXTURES / "property_direct_multi_property.html").read_text(encoding="utf-8")
    result = collect_from_html(site, html, run_id="r2")
    assert result.status.value == "ok"
    assert len(result.observations) == 1
    obs = result.observations[0]
    assert obs.title == "Shady Glen Apartments"
    assert obs.address_raw == "719 S. Weaver, Gainesville, TX 76240"
    assert obs.beds == 2.0
    assert obs.asking_rent == 825
    assert result.diagnostics.get("filtered_out") == 2


def test_name_filter_absent_emits_all_cards() -> None:
    """Without name_filter, all 3 cards in a multi-property page should be emitted."""
    site = PropertyDirectSite(
        name="Multi Property Site",
        url="https://example.com/apartments/",
        address="719 S. Weaver, Gainesville, TX 76240",
        selectors=MULTI_PROPERTY_SELECTORS,
        name_filter=None,
    )
    html = (FIXTURES / "property_direct_multi_property.html").read_text(encoding="utf-8")
    result = collect_from_html(site, html, run_id="r3")
    assert result.status.value == "ok"
    assert len(result.observations) == 3
    assert "filtered_out" not in result.diagnostics
