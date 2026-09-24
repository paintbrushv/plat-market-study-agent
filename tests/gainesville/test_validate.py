from __future__ import annotations

import datetime as dt

from etl.gainesville.dataclasses import RawObservation
from etl.gainesville.validate import validate_and_clean


def _obs(**kwargs: object) -> RawObservation:
    base = dict(
        observation_id="x", run_id="r", source="s", source_listing_id="sl",
        url="u", scraped_at=dt.datetime(2026, 5, 11, tzinfo=dt.UTC),
        listing_kind="sfr", address_raw="a", address_normalized="a",
        addr_norm_version=1, city=None, zip=None, lat=None, lon=None,
        beds=2.0, baths=1.0, sqft=900, asking_rent=1200,
        concessions_text=None, date_posted=None, date_available=None,
        title=None, body=None, raw_payload_path=None,
    )
    base.update(kwargs)
    return RawObservation(**base)  # type: ignore[arg-type]


def test_in_range_passes_unchanged() -> None:
    obs = _obs()
    cleaned, issues = validate_and_clean(obs)
    assert issues == []
    assert cleaned.asking_rent == 1200


def test_out_of_range_rent_nulled() -> None:
    cleaned, issues = validate_and_clean(_obs(asking_rent=50_000))
    assert cleaned.asking_rent is None
    assert "asking_rent_out_of_range" in issues


def test_out_of_range_beds_nulled() -> None:
    cleaned, issues = validate_and_clean(_obs(beds=99.0))
    assert cleaned.beds is None
    assert "beds_out_of_range" in issues


def test_negative_sqft_nulled() -> None:
    cleaned, issues = validate_and_clean(_obs(sqft=-100))
    assert cleaned.sqft is None
