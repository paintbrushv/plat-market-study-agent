from pathlib import Path

from etl.standardize_rent_roll import (
    _dedupe_physical_units,
    _apply_unit_type_mapping,
    generate_floorplan_summary,
    load_config,
    transform_rent_roll,
    infer_bed_type_from_floorplan,
    normalize_bed_type,
)


def test_floorplan_prefix_inference_ignores_variant_digits() -> None:
    assert normalize_bed_type("A4 Alt 2", {}, allow_numeric_fallback=False) == "A4 Alt 2"
    assert infer_bed_type_from_floorplan("A4 Alt 2", {"A": "1BR", "B": "2BR", "C": "3BR"}) == "1BR"
    assert infer_bed_type_from_floorplan("B1 Alt 1", {"A": "1BR", "B": "2BR", "C": "3BR"}) == "2BR"
    assert infer_bed_type_from_floorplan("C1", {"A": "1BR", "B": "2BR", "C": "3BR"}) == "3BR"


def test_crossings_mapping_skips_subheaders_and_future_applicants(tmp_path) -> None:
    # The original test ran against a live deal-room rent roll on the private
    # workstation. That file is not shipped (private data), so the test is
    # skipped with an explicit reason when the input is absent. Provide the
    # rent roll at the path below to exercise it.
    input_path = Path(
        "~/runs/deals/cross_in_at_hillcroft/raw_inputs/Crossings Rent Roll 4.16.26.xlsx"
    ).expanduser()
    if not input_path.exists():
        import pytest

        pytest.skip(
            "live Crossings rent roll not shipped with the public repo "
            "(private deal-room data); see tests/test_standardize_rent_roll.py"
        )
    records = transform_rent_roll(input_path, load_config("crossings_at_hillcroft"))

    assert len(records) == 300
    assert {r["bed_type"] for r in records} == {"1BR", "2BR"}
    assert {r["bath_count"] for r in records} == {1.0, 2.0}

    summary = {r["PlanCode"]: r for r in generate_floorplan_summary(records)}
    assert summary["c-a1-1x1"]["Units"] == 150
    assert summary["c-a1-1x1"]["BedType"] == "1BR"
    assert summary["c-a1-1x1"]["AvgMarketRent"] == 1341.0
    assert summary["c-b2-2x2"]["Units"] == 150
    assert summary["c-b2-2x2"]["BedType"] == "2BR"
    assert summary["c-b2-2x2"]["AvgMarketRent"] == 1576.33


def test_duplicate_future_rows_do_not_inflate_physical_unit_count() -> None:
    records = [
        {
            "unit_id": "2-225",
            "floorplan_code": "A1P",
            "status": "Vacant",
            "move_in_date": "",
            "lease_start": "",
            "lease_end": "",
            "lease_rent": 0,
            "total_rent": 0,
        },
        {
            "unit_id": "2-225",
            "floorplan_code": "A1P",
            "status": "Vacant",
            "move_in_date": "",
            "lease_start": "2026-05-22",
            "lease_end": "2026-05-22",
            "lease_rent": 1276,
            "total_rent": 1680.84,
        },
        {
            "unit_id": "8-826",
            "floorplan_code": "B2P",
            "status": "Notice",
            "move_in_date": "2025-03-29",
            "lease_start": "2025-03-29",
            "lease_end": "2026-05-28",
            "lease_rent": 1398,
            "total_rent": 1540,
        },
        {
            "unit_id": "8-826",
            "floorplan_code": "B2P",
            "status": "Notice",
            "move_in_date": "",
            "lease_start": "2026-06-20",
            "lease_end": "2026-06-20",
            "lease_rent": 1632,
            "total_rent": 1632,
        },
    ]

    deduped = _dedupe_physical_units(records)
    by_unit = {row["unit_id"]: row for row in deduped}

    assert len(deduped) == 2
    assert by_unit["2-225"]["lease_rent"] == 0
    assert by_unit["8-826"]["move_in_date"] == "2025-03-29"


def test_floorplan_mappings_alias_populates_bed_bath_and_sqft() -> None:
    bed_type, bath_count, sqft = _apply_unit_type_mapping(
        floorplan_code="B2P",
        bed_type="",
        bath_count=None,
        sqft=None,
        config={
            "floorplan_mappings": {
                "B2P": {
                    "bed_type": "2BR",
                    "bath_count": 2,
                    "sqft": 883,
                }
            }
        },
    )

    assert bed_type == "2BR"
    assert bath_count == 2
    assert sqft == 883
