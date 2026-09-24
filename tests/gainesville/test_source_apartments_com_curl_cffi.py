"""Tests for the curl-cffi fetch layer and updated HTML parser for Apartments.com.

These tests verify:
1. The new article-based placard format (fan-out per bed type for MF properties).
2. The basic-tier SFR placard format (single card, property-pricing layout).
3. collect_via_curl_cffi() with a mocked curl_cffi.requests.get.
4. Backward compatibility with the old <div class="placard"> format (fixture unchanged).
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from unittest.mock import MagicMock, patch

from etl.gainesville.dataclasses import Catchment, CollectionStatus
from etl.gainesville.sources.apartments_com import (
    Card,
    card_to_observations,
    collect_via_curl_cffi,
    parse_search_html,
)

FIXTURES = Path(__file__).parent / "fixtures"


# ---------------------------------------------------------------------------
# Parser tests — new format (article-based, bedRentBox fan-out)
# ---------------------------------------------------------------------------

def test_parse_new_format_mf_fans_out_per_bed() -> None:
    """Platinum/silver tier MF placard with 3 bedRentBox entries → 3 Cards."""
    html = (FIXTURES / "apartments_com_search_v2.html").read_text(encoding="utf-8")
    cards = parse_search_html(html)
    # Liberty Lofts: 3 bed types → 3 cards; heh99n0: basic tier → 1 card
    assert len(cards) == 4
    liberty_cards = [c for c in cards if c.listing_id.startswith("qezcg7y")]
    assert len(liberty_cards) == 3
    beds = sorted(c.beds_min for c in liberty_cards)  # type: ignore[arg-type]
    assert beds == [1.0, 2.0, 3.0]


def test_parse_new_format_mf_rents_match_bedrentbox() -> None:
    """Each fan-out card carries the correct per-bed rent."""
    html = (FIXTURES / "apartments_com_search_v2.html").read_text(encoding="utf-8")
    cards = parse_search_html(html)
    by_beds = {c.beds_min: c for c in cards if c.listing_id.startswith("qezcg7y")}
    assert by_beds[1.0].rent_min == 1150
    assert by_beds[2.0].rent_min == 1250
    assert by_beds[3.0].rent_min == 1495


def test_parse_new_format_mf_address_propagated() -> None:
    """Address from the property-address div propagates to all fan-out cards."""
    html = (FIXTURES / "apartments_com_search_v2.html").read_text(encoding="utf-8")
    cards = parse_search_html(html)
    liberty_cards = [c for c in cards if c.listing_id.startswith("qezcg7y")]
    for c in liberty_cards:
        assert c.address_raw == "400 S Culberson St, Gainesville, TX 76240"


def test_parse_new_format_basic_sfr_single_card() -> None:
    """Basic-tier SFR placard emits one Card with rent from property-pricing."""
    html = (FIXTURES / "apartments_com_search_v2.html").read_text(encoding="utf-8")
    cards = parse_search_html(html)
    sfr = next(c for c in cards if c.listing_id == "heh99n0")
    assert sfr.rent_min == 1900
    assert sfr.beds_min is None  # basic tier doesn't expose bed count in search card


def test_parse_new_format_basic_sfr_address_from_street_attr() -> None:
    """Basic-tier SFR uses data-streetaddress tag attribute when property-address absent."""
    html = (FIXTURES / "apartments_com_search_v2.html").read_text(encoding="utf-8")
    cards = parse_search_html(html)
    sfr = next(c for c in cards if c.listing_id == "heh99n0")
    assert sfr.address_raw == "317 Ritchey St"


# ---------------------------------------------------------------------------
# Backward compatibility — old <div class="placard"> format (original fixture)
# ---------------------------------------------------------------------------

def test_old_format_still_parses() -> None:
    """Original fixture with <div class='placard'> format still returns 2 cards."""
    html = (FIXTURES / "apartments_com_search.html").read_text(encoding="utf-8")
    cards = parse_search_html(html)
    assert len(cards) == 2


def test_old_format_ranged_card_no_fanout() -> None:
    """Old-format ranged card (1-2 bed, no bedRentBox) stays as one card."""
    html = (FIXTURES / "apartments_com_search.html").read_text(encoding="utf-8")
    cards = parse_search_html(html)
    tower = cards[0]
    assert tower.beds_min == 1.0
    assert tower.beds_max == 2.0
    assert tower.rent_min == 1250


# ---------------------------------------------------------------------------
# card_to_observations — fan-out cards produce correct observation
# ---------------------------------------------------------------------------

def test_fanout_card_to_observation_no_range_in_title() -> None:
    """A fan-out card (beds_min == beds_max) does not add range suffix to title."""
    card = Card(
        listing_id="qezcg7y-1br",
        url="https://www.apartments.com/the-liberty-lofts-gainesville-tx/qezcg7y/",
        name="The Liberty Lofts",
        address_raw="400 S Culberson St, Gainesville, TX 76240",
        beds_min=1.0,
        beds_max=1.0,
        baths_min=None,
        baths_max=None,
        sqft_min=None,
        sqft_max=None,
        rent_min=1150,
        rent_max=1150,
    )
    obs_list = card_to_observations(card, run_id="r1", scraped_at=dt.datetime(2026, 5, 11, tzinfo=dt.UTC))
    assert len(obs_list) == 1
    obs = obs_list[0]
    assert obs.beds == 1.0
    assert obs.asking_rent == 1150
    assert obs.title == "The Liberty Lofts"


# ---------------------------------------------------------------------------
# collect_via_curl_cffi — mocked integration test
# ---------------------------------------------------------------------------

def test_collect_via_curl_cffi_returns_observations() -> None:
    """collect_via_curl_cffi emits observations when curl_cffi.requests.get succeeds."""
    fixture_html = (FIXTURES / "apartments_com_search_v2.html").read_text(encoding="utf-8")

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.text = fixture_html

    catchment = Catchment("48097", ("76240",), (33.5, -97.4, 33.8, -96.8), None)

    with patch("etl.gainesville.sources.apartments_com._fetch_via_curl_cffi", return_value=fixture_html):
        result = collect_via_curl_cffi(catchment, run_id="test-run-01")

    assert result.status == CollectionStatus.OK
    # 4 cards from fixture: 3 Liberty Lofts fan-out + 1 basic SFR
    assert len(result.observations) == 4
    sources = {obs.source for obs in result.observations}
    assert sources == {"apartments_com"}


def test_collect_via_curl_cffi_network_failure() -> None:
    """Returns NETWORK_FAILED when the fetch helper returns None."""
    catchment = Catchment("48097", ("76240",), (33.5, -97.4, 33.8, -96.8), None)

    with patch("etl.gainesville.sources.apartments_com._fetch_via_curl_cffi", return_value=None):
        result = collect_via_curl_cffi(catchment, run_id="test-run-02")

    assert result.status == CollectionStatus.NETWORK_FAILED
    assert result.observations == []
