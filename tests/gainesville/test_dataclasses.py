from __future__ import annotations

import datetime as dt

from etl.gainesville.dataclasses import (
    Catchment,
    CollectionResult,
    CollectionStatus,
    RawObservation,
)


def test_catchment_holds_polygon_and_zips() -> None:
    catchment = Catchment(
        county_fips="48097",
        zip_codes=("76240", "76252"),
        bbox=(33.5, -97.4, 33.8, -97.0),
        city_limits_geojson_path=None,
    )
    assert catchment.county_fips == "48097"
    assert "76240" in catchment.zip_codes
    assert catchment.bbox == (33.5, -97.4, 33.8, -97.0)


def test_raw_observation_required_fields() -> None:
    obs = RawObservation(
        observation_id="abc123",
        run_id="2026-W19_gainesville",
        source="craigslist",
        source_listing_id="cl-7654321",
        url="https://dallas.craigslist.org/foo/123.html",
        scraped_at=dt.datetime(2026, 5, 9, 12, 0, tzinfo=dt.UTC),
        listing_kind="sfr",
        address_raw="123 Main St, Gainesville, TX 76240",
        address_normalized="123 main st",
        addr_norm_version=1,
        city="Gainesville",
        zip="76240",
        lat=33.626,
        lon=-97.133,
        beds=3.0,
        baths=2.0,
        sqft=1450,
        asking_rent=1500,
        concessions_text=None,
        date_posted=dt.date(2026, 5, 8),
        date_available=None,
        title="3BR/2BA in Gainesville",
        body="Nice house",
        raw_payload_path="reports/gainesville-tx/sfr/listings_history/2026-W19_gainesville/craigslist.parquet",
    )
    assert obs.beds == 3.0
    assert obs.zip == "76240"


def test_collection_result_status_values() -> None:
    assert CollectionStatus.OK.value == "ok"
    assert CollectionStatus.CAPTCHA_BLOCKED.value == "captcha_blocked"
    assert CollectionStatus.ZERO_RESULTS_SUSPICIOUS.value == "zero_results_suspicious"
    assert CollectionStatus.DROP_RATE_ANOMALY.value == "drop_rate_anomaly"
    assert CollectionStatus.PARSER_DRIFT.value == "parser_drift"
    assert CollectionStatus.NETWORK_FAILED.value == "network_failed"


def test_collection_result_carries_diagnostics() -> None:
    result = CollectionResult(
        observations=[],
        status=CollectionStatus.OK,
        diagnostics={"pages_scraped": 3, "field_coverage": {"asking_rent": 0.95}},
    )
    assert result.status == CollectionStatus.OK
    assert result.diagnostics["pages_scraped"] == 3
