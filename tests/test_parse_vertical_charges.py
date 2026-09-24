from pathlib import Path
from datetime import datetime

import pandas as pd

from etl.parse_vertical_charges import load_config, parse_rent_roll
import pytest


def test_dylan_vertical_charge_config_parses_resman_layout(tmp_path: Path) -> None:
    pytest.skip(reason="requires a real-property config that is not shipped in the public tree")
    path = tmp_path / "Rent-Roll-Dylan-4.24.2026.xlsx"
    rows = [[""] * 41 for _ in range(18)]
    rows[0][40] = "keep trailing columns"
    rows[2][10] = "Dylan Apartments"
    rows[9][0] = "Current"
    rows[10][0] = "101"
    rows[10][2] = "B1U -2x1"
    rows[10][4] = 781
    rows[10][5] = "Resident One"
    rows[10][11] = "C"
    rows[10][13] = 1300
    rows[10][20] = "Rent"
    rows[10][23] = 1315
    rows[11][20] = "Cable/Internet Income"
    rows[11][23] = 75
    rows[12][20] = "Valet Trash"
    rows[12][23] = 20
    rows[13][0] = "102"
    rows[13][2] = "A1-1X1"
    rows[13][4] = 650
    rows[13][5] = "VACANT"
    rows[13][11] = "V"
    rows[13][13] = 1100
    rows[14][20] = "Rent"
    rows[14][23] = 0
    rows[15][0] = "Future Residents/Applicants"
    rows[16][0] = "201"
    rows[16][2] = "B1-2X1"
    pd.DataFrame(rows).to_excel(path, header=False, index=False)

    records = parse_rent_roll(path, load_config("example"))

    assert [row["unit_id"] for row in records] == ["101", "102"]
    assert records[0]["bed_type"] == "2BR"
    assert records[0]["lease_rent"] == 1315
    assert records[0]["utility_reimbursement"] == 95
    assert records[1]["status"] == "Vacant"


def test_grant_at_valley_ranch_config_parses_s2_vertical_charges(tmp_path: Path) -> None:
    pytest.skip(reason="requires a real-property config that is not shipped in the public tree")
    path = tmp_path / "Grant at Valley Ranch - RR - 4.28.26.xlsx"
    rows = [[""] * 35 for _ in range(18)]
    rows[1][0] = "Grant Valley Ranch"
    rows[2][0] = "S2 Residential"
    rows[3][0] = "Rent Roll"
    rows[8][1] = "Unit"
    rows[8][2] = "Type"
    rows[8][4] = "Sq. Feet"
    rows[8][10] = "Status"
    rows[8][12] = "Market Rent"
    rows[8][19] = "Description"
    rows[8][22] = "Amount"
    rows[9][0] = "101"
    rows[9][2] = "B1R"
    rows[9][4] = 937
    rows[9][5] = "Resident One"
    rows[9][10] = "C"
    rows[9][12] = 1906
    rows[9][19] = "Rent"
    rows[9][22] = 1930
    rows[9][26] = datetime(2025, 1, 1)
    rows[9][28] = datetime(2026, 1, 1)
    rows[9][29] = datetime(2027, 1, 1)
    rows[10][19] = "CAM Fee"
    rows[10][22] = 160
    rows[11][19] = "Total"
    rows[11][22] = 2090
    rows[13][0] = "102"
    rows[13][2] = "A1"
    rows[13][4] = 619
    rows[13][5] = "VACANT"
    rows[13][10] = "V"
    rows[13][12] = 1175
    rows[13][19] = "Rent"
    rows[13][22] = 0
    rows[13][26] = datetime(2025, 2, 1)
    rows[13][28] = datetime(2026, 2, 1)
    rows[13][29] = datetime(2027, 2, 1)
    rows[14][0] = "Total Charges"
    rows[15][0] = "A1"
    rows[15][3] = "Occupied"
    rows[15][15] = 15
    pd.DataFrame(rows).to_excel(path, header=False, index=False)

    records = parse_rent_roll(path, load_config("example"))

    assert [row["unit_id"] for row in records] == ["101", "102"]
    assert records[0]["bed_type"] == "2BR"
    assert records[0]["market_rent"] == 1906
    assert records[0]["lease_rent"] == 1930
    assert records[0]["utility_reimbursement"] == 160
    assert records[1]["bed_type"] == "1BR"
    assert records[1]["status"] == "Vacant"


def test_example_floorplan_mapping_overrides_b_prefix_bath_count_and_keeps_charges(tmp_path: Path) -> None:
    path = tmp_path / "The Example - Rent Roll - 5.31.26.xlsx"
    rows = [[""] * 35 for _ in range(14)]
    rows[1][0] = "The Example"
    rows[3][0] = "Rent Roll"
    rows[8][0] = "Unit"
    rows[8][2] = "Type"
    rows[8][4] = "Sq. Feet"
    rows[8][10] = "Status"
    rows[8][12] = "Market Rent"
    rows[8][19] = "Description"
    rows[8][22] = "Amount"
    rows[9][0] = "101"
    rows[9][2] = "B1"
    rows[9][4] = 999
    rows[9][5] = "Resident One"
    rows[9][10] = "C"
    rows[9][12] = 1500
    rows[9][19] = "Rent"
    rows[9][22] = 1525
    rows[9][26] = datetime(2025, 3, 1)
    rows[9][28] = datetime(2026, 3, 1)
    rows[9][29] = datetime(2027, 3, 1)
    rows[10][19] = "Water/Sewer"
    rows[10][22] = 85
    rows[12][19] = "Total"
    rows[12][22] = 1610
    pd.DataFrame(rows).to_excel(path, header=False, index=False)

    records = parse_rent_roll(path, load_config("example"))

    assert len(records) == 1
    assert records[0]["floorplan_code"] == "B1"
    assert records[0]["bed_type"] == "2BR"
    assert records[0]["bath_count"] == 1.0
    assert records[0]["sqft"] == 837
    assert records[0]["lease_rent"] == 1525
    assert records[0]["utility_reimbursement"] == 85
    assert records[0]["total_rent"] == 1610
