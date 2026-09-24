from __future__ import annotations

import datetime as dt
from pathlib import Path

from etl.gainesville.sources.apartments_com import Card, card_to_observations, parse_search_html

FIXTURES = Path(__file__).parent / "fixtures"


def test_parse_search_html_extracts_two_cards() -> None:
    html = (FIXTURES / "apartments_com_search.html").read_text(encoding="utf-8")
    cards = parse_search_html(html)
    assert len(cards) == 2
    tower = cards[0]
    assert tower.name == "Tower View"
    assert tower.address_raw == "500 Pine Drive, Gainesville, TX 76240"
    assert tower.beds_min == 1.0
    assert tower.beds_max == 2.0
    assert tower.baths_min == 1.0
    assert tower.baths_max == 2.0
    assert tower.sqft_min == 650
    assert tower.sqft_max == 1100
    assert tower.rent_min == 1250
    assert tower.rent_max == 1650


def test_single_value_fields_parse_as_min_eq_max() -> None:
    html = (FIXTURES / "apartments_com_search.html").read_text(encoding="utf-8")
    cards = parse_search_html(html)
    oak = cards[1]
    assert oak.beds_min == oak.beds_max == 2.0
    assert oak.baths_min == oak.baths_max == 1.0
    assert oak.rent_min == oak.rent_max == 1100
    assert oak.sqft_min == oak.sqft_max == 800


def test_card_to_observations_no_fanout_for_range_card() -> None:
    """A card with 1-2 bed range produces ONE observation with min values
    and a title that includes the range. We don't fan out because the
    search card doesn't reveal per-bed sqft/rent.
    """
    card = Card(
        listing_id="ap-XYZ",
        url="https://example.com",
        name="Tower View",
        address_raw="500 Pine Drive, Gainesville, TX 76240",
        beds_min=1.0,
        beds_max=2.0,
        baths_min=1.0,
        baths_max=2.0,
        sqft_min=650,
        sqft_max=1100,
        rent_min=1250,
        rent_max=1650,
    )
    scraped_at = dt.datetime(2026, 5, 11, tzinfo=dt.UTC)
    obs_list = card_to_observations(card, run_id="r1", scraped_at=scraped_at)
    assert len(obs_list) == 1
    obs = obs_list[0]
    assert obs.beds == 1.0
    assert obs.asking_rent == 1250  # conservative min
    assert "1-2BR" in (obs.title or "") or "1-2 BR" in (obs.title or "")


def test_card_to_observations_single_bed_no_range_in_title() -> None:
    """A card with fixed bed count produces ONE observation with no range suffix."""
    card = Card(
        listing_id="ap-ABC",
        url="https://example.com",
        name="Oak Villa",
        address_raw="200 Oak Street, Gainesville, TX 76240",
        beds_min=2.0,
        beds_max=2.0,
        baths_min=1.0,
        baths_max=1.0,
        sqft_min=800,
        sqft_max=800,
        rent_min=1100,
        rent_max=1100,
    )
    scraped_at = dt.datetime(2026, 5, 11, tzinfo=dt.UTC)
    obs_list = card_to_observations(card, run_id="r1", scraped_at=scraped_at)
    assert len(obs_list) == 1
    obs = obs_list[0]
    assert obs.beds == 2.0
    assert obs.asking_rent == 1100
    assert obs.title == "Oak Villa"
