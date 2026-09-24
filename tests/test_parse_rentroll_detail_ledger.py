from pathlib import Path

import openpyxl

from etl.parse_rentroll_detail_ledger import floorplan_summary, parse_file


def _write_fixture(path: Path) -> None:
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "RentRollDetail"
    sheet.append([None] * 11)
    sheet.append(
        [
            "Unit",
            "Type",
            "Market",
            "Name",
            "Lease Dates",
            "Notice",
            "Net Change\nIn Balance",
            "Resident\nBalance",
            "Description",
            "Charge Amount",
            "Credit Amount",
        ]
    )
    sheet.append(
        [
            "20-101",
            "B1 B1, W/D",
            1393,
            "Resident, Test T0001 01/01/2025",
            "01/01/2026 12/31/2026",
            None,
            None,
            None,
            "RENT-Rent",
            1350,
            None,
        ]
    )
    sheet.append([None, None, None, None, None, None, None, None, "GAR-Garage Rental #", 125, None])
    sheet.append([None, None, None, None, None, None, None, None, "PETR-Pet Rent", 50, None])
    sheet.append([None, None, None, None, None, None, None, None, "LTOL-Loss To Lease In Force", None, 43])
    sheet.append(["20-102", "A1 ", 1188, "Vacant Unit ", " ", None, None, None, "RENT-Rent", 1188, None])
    sheet.append([None, None, None, None, None, None, None, None, "VAC -Vacancy Loss", None, 1188])
    sheet.append(["Total", None, None, None, None, None, None, None, None, 2663, 1231])
    workbook.save(path)


def test_parse_ledger_style_rent_roll_groups_charge_rows(tmp_path: Path) -> None:
    fixture = tmp_path / "rent_roll.xlsx"
    _write_fixture(fixture)

    records = parse_file(fixture, asset="Example")

    assert len(records) == 2
    occupied = records[0]
    assert occupied["unit_id"] == "20-101"
    assert occupied["floorplan_code"] == "B1"
    assert occupied["bed_type"] == "2BR"
    assert occupied["bath_count"] == 2.0
    assert occupied["sqft"] == 957
    assert occupied["lease_rent"] == 1350
    assert occupied["parking_rent"] == 125
    assert occupied["pet_rent"] == 50
    assert occupied["concessions"] == 0.0
    assert occupied["lease_start"] == "2026-01-01"
    assert occupied["lease_end"] == "2026-12-31"

    vacant = records[1]
    assert vacant["status"] == "Vacant"
    assert vacant["lease_rent"] == 0.0
    assert vacant["base_rent"] == 0.0

    summary = {row["PlanCode"]: row for row in floorplan_summary(records)}
    assert summary["A1"]["Units"] == 1
    assert summary["B1"]["OccupiedUnits"] == 1


def test_loss_to_lease_rows_are_not_recurring_income_or_concessions(tmp_path: Path) -> None:
    fixture = tmp_path / "rent_roll.xlsx"
    _write_fixture(fixture)

    records = parse_file(fixture, asset="Example")

    assert records[0]["other_income"] == 0.0
    assert records[1]["concessions"] == 0.0
