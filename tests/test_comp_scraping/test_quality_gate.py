from __future__ import annotations

import pytest
from etl.comp_scraping import quality_gate


@pytest.mark.parametrize(
    "units,expected",
    [
        (None, 1),
        (0, 1),
        (1, 1),
        (27, 1),  # boutique
        (59, 2),
        (220, 8),  # ceiling
        (404, 8),  # capped at 8
    ],
)
def test_min_threshold(units: int | None, expected: int) -> None:
    assert quality_gate.min_threshold(units) == expected


def _payload(plans: list[dict]) -> dict:
    return {
        "platform": "test",
        "floorplans": plans,
        "units": [],
        "specials": [],
    }


def test_passes_above_threshold() -> None:
    payload = _payload(
        [
            {"floorplan_name": "A1", "rent_min": 1500, "rent_max": 1600},
            {"floorplan_name": "A2", "rent_min": 1700, "rent_max": 1700},
        ]
    )
    assert quality_gate.passes(payload, min_plans=2) is True


def test_fails_below_threshold() -> None:
    payload = _payload(
        [{"floorplan_name": "A1", "rent_min": 1500, "rent_max": 1600}]
    )
    assert quality_gate.passes(payload, min_plans=2) is False

def test_single_priced_floorplan_still_fails_minimum_market_coverage() -> None:
    single_priced_plan = _payload(
        [{"floorplan_name": "A1", "rent_min": 1500, "rent_max": 1600}]
    )
    two_priced_plans = _payload(
        [
            {"floorplan_name": "A1", "rent_min": 1500, "rent_max": 1600},
            {"floorplan_name": "A2", "rent_min": 1700, "rent_max": 1750},
        ]
    )

    assert quality_gate.passes(single_priced_plan, min_plans=1) is False
    assert quality_gate.passes(two_priced_plans, min_plans=1) is True


def test_fails_when_all_rents_zero() -> None:
    payload = _payload(
        [
            {"floorplan_name": "A1", "rent_min": 0, "rent_max": 0},
            {"floorplan_name": "A2", "rent_min": 0, "rent_max": 0},
        ]
    )
    assert quality_gate.passes(payload, min_plans=1) is False


def test_invalid_payload_fails() -> None:
    assert quality_gate.passes("not a dict", min_plans=1) is False  # type: ignore[arg-type]
    assert quality_gate.passes({}, min_plans=1) is False
    assert quality_gate.passes({"floorplans": "string"}, min_plans=1) is False  # type: ignore[dict-item]
