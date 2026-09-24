from __future__ import annotations

from pathlib import Path

from etl.gainesville.dataclasses import CollectionStatus
from etl.gainesville.sources.zillow import detect_soft_block, parse_search_html

FIXTURES = Path(__file__).parent / "fixtures"


def test_detect_soft_block_captcha() -> None:
    html = (FIXTURES / "zillow_captcha.html").read_text(encoding="utf-8")
    assert detect_soft_block(html) == CollectionStatus.CAPTCHA_BLOCKED


def test_detect_soft_block_real_results_returns_none() -> None:
    html = (FIXTURES / "zillow_search.html").read_text(encoding="utf-8")
    assert detect_soft_block(html) is None


def test_detect_soft_block_zero_results_text() -> None:
    html = (
        '<html><body><div class="result-list-container">'
        "<h2>No matching results</h2></div></body></html>"
    )
    # Empty results page in a known-rental ZIP -> suspicious.
    assert detect_soft_block(html) == CollectionStatus.ZERO_RESULTS_SUSPICIOUS


def test_parse_search_html_extracts_listings() -> None:
    html = (FIXTURES / "zillow_search.html").read_text(encoding="utf-8")
    cards = parse_search_html(html)
    assert len(cards) == 2
    house = cards[0]
    assert house.zpid == "12345678"
    assert house.beds == 3.0
    assert house.baths == 2.0
    assert house.sqft == 1450
    assert house.rent == 1500
    assert house.home_type == "House for rent"
    assert house.address_raw == "123 Main St, Gainesville, TX 76240"


def test_classify_townhouse_before_house() -> None:
    """Townhouse must match before the generic 'house' substring check."""
    from etl.gainesville.sources.zillow import _classify_kind

    assert _classify_kind("Townhouse for rent") == "duplex"
    assert _classify_kind("Duplex for rent") == "duplex"
    assert _classify_kind("House for rent") == "sfr"
    assert _classify_kind("Apartment") == "mf"
    assert _classify_kind(None) == "unknown"


import pytest


@pytest.mark.parametrize(
    "home_type,expected",
    [
        # UPPERCASE_UNDERSCORE constants from Zillow __NEXT_DATA__ hdpData.homeInfo.homeType
        ("SINGLE_FAMILY", "sfr"),
        ("MANUFACTURED", "sfr"),
        ("MULTI_FAMILY", "mf"),
        ("APARTMENT", "mf"),
        ("CONDO", "mf"),
        ("TOWNHOUSE", "duplex"),
        ("DUPLEX", "duplex"),
        ("LOT", "unknown"),
        ("HOME_TYPE_UNKNOWN", "unknown"),
        # None sentinel
        (None, "unknown"),
        # Legacy HTML phrases (parse_search_html / hand-crafted fixtures)
        ("House for rent", "sfr"),
        ("Townhouse for rent", "duplex"),
        ("Apartment", "mf"),
        ("Condo for rent", "mf"),
        ("Manufactured home", "sfr"),
    ],
)
def test_classify_zillow_json_constants(home_type: str | None, expected: str) -> None:
    """_classify_kind handles both __NEXT_DATA__ UPPERCASE constants and HTML phrases."""
    from etl.gainesville.sources.zillow import _classify_kind

    assert _classify_kind(home_type) == expected, f"_classify_kind({home_type!r}) should be {expected!r}"
