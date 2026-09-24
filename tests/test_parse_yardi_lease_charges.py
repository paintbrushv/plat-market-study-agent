import pandas as pd

from etl.parse_yardi_lease_charges import generate_floorplan_summary, load_config, parse_rent_roll
import pytest


def test_yardi_lease_charges_skips_charge_code_summary_rows(monkeypatch, tmp_path) -> None:
    rows = [
        ["Rent Roll with Lease Charges", None, None, None, None, None, None, None, None, None, None, None],
        ["Current/Notice/Vacant Residents", None, None, None, None, None, None, None, None, None, None, None],
        ["0111", "A1", 709, "t001", "Resident One", 1275, "RENT", 1200, None, None, "2025-01-01", "2026-01-01"],
        [None, None, None, None, None, None, "PETRENT", 25, None, None, None, None],
        ["UTRSH", None, None, None, None, None, None, None, None, None, None, None],
        ["GARAGE", None, None, None, None, None, None, None, None, None, None, None],
    ]
    df = pd.DataFrame(rows)
    monkeypatch.setattr(pd, "read_excel", lambda *_args, **_kwargs: df)

    records = parse_rent_roll(tmp_path / "Marquis Rent Roll.xlsx", config=None)

    assert [r["unit_id"] for r in records] == ["0111"]
    assert records[0]["lease_rent"] == 1200
    assert records[0]["pet_rent"] == 25

    summary = generate_floorplan_summary(records)
    assert summary == [
        {
            "PlanCode": "A1",
            "BedType": "",
            "Units": 1,
            "SqFt": 709,
            "AvgMarketRent": 1275.0,
        }
    ]


def test_carmen_celine_yardi_configs_recover_wc_bed_bath(monkeypatch, tmp_path) -> None:
    pytest.skip(reason="requires a real-property config that is not shipped in the public tree")
    rows = [
        ["Rent Roll with Lease Charges", None, None, None, None, None, None, None, None, None, None, None],
        ["The Carmen (59030)", None, None, None, None, None, None, None, None, None, None, None],
        ["As Of = 05/04/2026", None, None, None, None, None, None, None, None, None, None, None],
        ["Current/Notice/Vacant Residents", None, None, None, None, None, None, None, None, None, None, None],
        ["0101", "WC4A1r", 550, "t1", "Resident One", 812, "RENT", 812, None, None, "2026-04-16", "2027-04-15"],
        ["0102", "WC4B1", 700, "t2", "Resident Two", 872, "RENT", 872, None, None, "2026-04-16", "2027-04-15"],
        ["0103", "WC4B2r", 784, "t3", "Resident Three", 1253, "RENT", 1253, None, None, "2026-04-16", "2027-04-15"],
    ]
    monkeypatch.setattr(pd, "read_excel", lambda *_args, **_kwargs: pd.DataFrame(rows))

    records = parse_rent_roll(tmp_path / "Carmen RR 5.4.26.xlsx", config=load_config("the_carmen"))

    by_plan = {row["floorplan_code"]: row for row in records}
    assert by_plan["WC4A1r"]["bed_type"] == "1BR"
    assert by_plan["WC4A1r"]["bath_count"] == "1"
    assert by_plan["WC4B1"]["bed_type"] == "2BR"
    assert by_plan["WC4B1"]["bath_count"] == "1"
    assert by_plan["WC4B2r"]["bed_type"] == "2BR"
    assert by_plan["WC4B2r"]["bath_count"] == "2"
