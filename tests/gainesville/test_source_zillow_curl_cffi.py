"""Tests for the curl-cffi fetch layer and __NEXT_DATA__ JSON parser for Zillow.

These tests verify:
1. parse_next_data_json() extracts listings from __NEXT_DATA__ JSON blob.
2. Listings with unformattedPrice=None (contact-landlord) produce rent=None.
3. collect_via_curl_cffi() with a mocked _fetch_via_curl_cffi.
4. detect_soft_block() still works against the __NEXT_DATA__ page (no false triggers).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from etl.gainesville.dataclasses import Catchment, CollectionStatus
from etl.gainesville.sources.zillow import (
    collect_via_curl_cffi,
    detect_soft_block,
    parse_next_data_json,
)

FIXTURES = Path(__file__).parent / "fixtures"


# ---------------------------------------------------------------------------
# parse_next_data_json — JSON extraction from __NEXT_DATA__
# ---------------------------------------------------------------------------

def test_parse_next_data_extracts_listings() -> None:
    """__NEXT_DATA__ fixture yields 3 ZillowCards with correct fields."""
    html = (FIXTURES / "zillow_next_data.html").read_text(encoding="utf-8")
    cards = parse_next_data_json(html)
    assert len(cards) == 3


def test_parse_next_data_contact_landlord_rent_is_none() -> None:
    """unformattedPrice=None (contact landlord) → rent=None, not zero."""
    html = (FIXTURES / "zillow_next_data.html").read_text(encoding="utf-8")
    cards = parse_next_data_json(html)
    woodglen = next(c for c in cards if c.zpid == "461016294")
    assert woodglen.rent is None
    assert woodglen.beds == 1.0


def test_parse_next_data_priced_listing_has_rent() -> None:
    """Listing with unformattedPrice → rent as integer."""
    html = (FIXTURES / "zillow_next_data.html").read_text(encoding="utf-8")
    cards = parse_next_data_json(html)
    vintage = next(c for c in cards if c.zpid == "225101218")
    assert vintage.rent == 2400
    assert vintage.beds == 3.0
    assert vintage.sqft == 1705


def test_parse_next_data_detail_url_is_absolute() -> None:
    """detailUrl should be an absolute URL."""
    html = (FIXTURES / "zillow_next_data.html").read_text(encoding="utf-8")
    cards = parse_next_data_json(html)
    for c in cards:
        assert c.url.startswith("http"), f"Non-absolute URL: {c.url}"


def test_parse_next_data_returns_empty_for_no_script() -> None:
    """HTML without __NEXT_DATA__ returns empty list (no crash)."""
    cards = parse_next_data_json("<html><body>No data here</body></html>")
    assert cards == []


def test_parse_next_data_returns_empty_for_missing_key_path() -> None:
    """__NEXT_DATA__ with unexpected structure returns empty list."""
    import json
    malformed = json.dumps({"props": {"pageProps": {}}})
    html = f'<script id="__NEXT_DATA__" type="application/json">{malformed}</script>'
    cards = parse_next_data_json(html)
    assert cards == []


# ---------------------------------------------------------------------------
# detect_soft_block — should NOT fire on the __NEXT_DATA__ page
# ---------------------------------------------------------------------------

def test_detect_soft_block_returns_none_for_next_data_page() -> None:
    """The __NEXT_DATA__ fixture (real listing page) must not trigger soft-block."""
    html = (FIXTURES / "zillow_next_data.html").read_text(encoding="utf-8")
    assert detect_soft_block(html) is None


# ---------------------------------------------------------------------------
# collect_via_curl_cffi — mocked integration test
# ---------------------------------------------------------------------------

def test_collect_via_curl_cffi_returns_observations() -> None:
    """collect_via_curl_cffi emits observations when _fetch_via_curl_cffi succeeds."""
    fixture_html = (FIXTURES / "zillow_next_data.html").read_text(encoding="utf-8")
    catchment = Catchment("48097", ("76240",), (33.5, -97.4, 33.8, -96.8), None)

    with patch("etl.gainesville.sources.zillow._fetch_via_curl_cffi", return_value=fixture_html):
        result = collect_via_curl_cffi(catchment, run_id="test-run-01")

    assert result.status == CollectionStatus.OK
    assert len(result.observations) == 3
    sources = {obs.source for obs in result.observations}
    assert sources == {"zillow"}


def test_collect_via_curl_cffi_network_failure() -> None:
    """Returns NETWORK_FAILED when fetch returns None."""
    catchment = Catchment("48097", ("76240",), (33.5, -97.4, 33.8, -96.8), None)

    with patch("etl.gainesville.sources.zillow._fetch_via_curl_cffi", return_value=None):
        result = collect_via_curl_cffi(catchment, run_id="test-run-02")

    assert result.status == CollectionStatus.NETWORK_FAILED
    assert result.observations == []


def test_parse_next_data_listing_kind_from_hdp_home_info() -> None:
    """parse_next_data_json extracts homeType from hdpData.homeInfo and classifies correctly.

    The fixture uses real __NEXT_DATA__ structure: top-level homeType=null (as in
    production), with homeType in hdpData.homeInfo.homeType (UPPERCASE_UNDERSCORE).
    """
    from etl.gainesville.sources.zillow import card_to_observation
    import datetime as dt

    html = (FIXTURES / "zillow_next_data.html").read_text(encoding="utf-8")
    cards = parse_next_data_json(html)
    # Fixture: 461016294 = APARTMENT, 225101218 = SINGLE_FAMILY, 87244093 = SINGLE_FAMILY
    kinds = {c.zpid: c.home_type for c in cards}
    assert kinds["461016294"] == "APARTMENT"
    assert kinds["225101218"] == "SINGLE_FAMILY"
    assert kinds["87244093"] == "SINGLE_FAMILY"

    # Verify card_to_observation classifies correctly
    now = dt.datetime.now(dt.UTC)
    obs_sfr = card_to_observation(next(c for c in cards if c.zpid == "225101218"), "test-run", now)
    obs_mf = card_to_observation(next(c for c in cards if c.zpid == "461016294"), "test-run", now)
    assert obs_sfr.listing_kind == "sfr"
    assert obs_mf.listing_kind == "mf"


def test_collect_via_curl_cffi_zero_results_suspicious() -> None:
    """Returns ZERO_RESULTS_SUSPICIOUS when __NEXT_DATA__ has empty listResults."""
    import json
    empty_data = json.dumps({
        "props": {"pageProps": {"searchPageState": {"cat1": {"searchResults": {"listResults": []}}}}}
    })
    fixture_html = f'<script id="__NEXT_DATA__" type="application/json">{empty_data}</script>'
    catchment = Catchment("48097", ("76240",), (33.5, -97.4, 33.8, -96.8), None)

    with patch("etl.gainesville.sources.zillow._fetch_via_curl_cffi", return_value=fixture_html):
        result = collect_via_curl_cffi(catchment, run_id="test-run-03")

    assert result.status == CollectionStatus.ZERO_RESULTS_SUSPICIOUS
    assert result.observations == []
