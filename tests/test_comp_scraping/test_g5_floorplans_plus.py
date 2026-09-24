from __future__ import annotations

from etl.collect_comps_snapshot import _backfill_g5_floorplan_rents_from_units


def test_backfill_g5_floorplan_rents_from_unit_rates() -> None:
    floorplans = [
        {
            "floorplan_id": 101,
            "floorplan_name": "A1",
            "rent_min": 0.0,
            "rent_max": 0.0,
        },
        {
            "floorplan_id": 202,
            "floorplan_name": "B1",
            "rent_min": 1300.0,
            "rent_max": 1350.0,
        },
    ]
    units = [
        {"floorplan_id": 101, "unit_number": "1", "rent": 954.0},
        {"floorplan_id": 101, "unit_number": "2", "rent": 999.0},
        {"floorplan_id": 202, "unit_number": "3", "rent": 1400.0},
    ]

    _backfill_g5_floorplan_rents_from_units(floorplans, units)

    assert floorplans[0]["rent_min"] == 954.0
    assert floorplans[0]["rent_max"] == 999.0
    assert floorplans[1]["rent_min"] == 1300.0
    assert floorplans[1]["rent_max"] == 1350.0
