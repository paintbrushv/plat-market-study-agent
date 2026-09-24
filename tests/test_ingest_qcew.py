"""Unit tests for etl/ingest_qcew.py.

Network-free: tests cover area-code conversion, record filtering, and
normalization shape against a hand-rolled QCEW response fixture.
"""

from __future__ import annotations

import pytest
from etl.ingest_qcew import (
    cbsa_to_qcew_area,
    filter_records,
    normalize_records,
)

# Trimmed shape of a real QCEW Open Data row.
SAMPLE_QCEW_ROWS = [
    {
        "area_fips": "C1910",
        "own_code": "5",
        "industry_code": "10",
        "year": "2024",
        "qtr": "3",
        "qtrly_estabs": "120000",
        "month1_emplvl": "3500000",
        "month2_emplvl": "3510000",
        "month3_emplvl": "3520000",
        "total_qtrly_wages": "70000000000",
        "avg_wkly_wage": "1500",
    },
    {
        "area_fips": "C1910",
        "own_code": "0",
        "industry_code": "10",
        "year": "2024",
        "qtr": "3",
        "qtrly_estabs": "130000",
        "month1_emplvl": "3700000",
        "month2_emplvl": "3710000",
        "month3_emplvl": "3720000",
        "total_qtrly_wages": "75000000000",
        "avg_wkly_wage": "1480",
    },
    # Sector row that should be filtered out by default (industry != 10).
    {
        "area_fips": "C1910",
        "own_code": "5",
        "industry_code": "23",  # Construction
        "year": "2024",
        "qtr": "3",
        "qtrly_estabs": "8000",
        "month1_emplvl": "200000",
        "month2_emplvl": "201000",
        "month3_emplvl": "202000",
        "total_qtrly_wages": "4000000000",
        "avg_wkly_wage": "1700",
    },
    # Government row that should be filtered out by default (own_code 2 not in defaults).
    {
        "area_fips": "C1910",
        "own_code": "2",
        "industry_code": "10",
        "year": "2024",
        "qtr": "3",
        "qtrly_estabs": "500",
        "month1_emplvl": "100000",
        "month2_emplvl": "100000",
        "month3_emplvl": "100000",
        "total_qtrly_wages": "1500000000",
        "avg_wkly_wage": "1100",
    },
]


class TestCbsaToQcewArea:
    def test_dallas_fort_worth(self) -> None:
        assert cbsa_to_qcew_area("19100") == "C1910"

    def test_austin(self) -> None:
        assert cbsa_to_qcew_area("12420") == "C1242"

    def test_huntsville(self) -> None:
        assert cbsa_to_qcew_area("26620") == "C2662"

    def test_birmingham(self) -> None:
        assert cbsa_to_qcew_area("13820") == "C1382"

    def test_already_formatted(self) -> None:
        assert cbsa_to_qcew_area("C1910") == "C1910"

    def test_already_formatted_lowercase(self) -> None:
        assert cbsa_to_qcew_area("c1910") == "C1910"

    def test_rejects_micropolitan_or_county_codes(self) -> None:
        # CBSA codes for MSAs always end in 0; non-zero last digit is not an MSA CBSA.
        with pytest.raises(ValueError):
            cbsa_to_qcew_area("19101")

    def test_rejects_garbage(self) -> None:
        with pytest.raises(ValueError):
            cbsa_to_qcew_area("hello")


class TestFilterRecords:
    def test_default_filters_to_industry_10_total_and_private(self) -> None:
        kept = filter_records(SAMPLE_QCEW_ROWS)
        own_codes = {r["own_code"] for r in kept}
        industry_codes = {r["industry_code"] for r in kept}
        assert own_codes == {"0", "5"}
        assert industry_codes == {"10"}

    def test_can_request_specific_sector(self) -> None:
        kept = filter_records(SAMPLE_QCEW_ROWS, industries=("23",), ownerships=("5",))
        assert len(kept) == 1
        assert kept[0]["industry_code"] == "23"
        assert kept[0]["own_code"] == "5"


class TestNormalizeRecords:
    def test_emits_one_record_per_metric(self) -> None:
        kept = filter_records(SAMPLE_QCEW_ROWS)
        records = normalize_records(
            kept,
            msa_code="19100",
            area_fips="C1910",
            year=2024,
            qtr=3,
            source_id="BLS-QCEW",
        )
        # 2 input rows (private + total covered) x 4 metrics = 8 records.
        assert len(records) == 8
        metrics = {r["metric"] for r in records}
        assert metrics == {
            "qcew_employment_month3",
            "qcew_avg_weekly_wage",
            "qcew_total_quarterly_wages",
            "qcew_qtrly_estabs",
        }

    def test_record_shape(self) -> None:
        records = normalize_records(
            [SAMPLE_QCEW_ROWS[0]],
            msa_code="19100",
            area_fips="C1910",
            year=2024,
            qtr=3,
            source_id="BLS-QCEW",
        )
        emp = next(r for r in records if r["metric"] == "qcew_employment_month3")
        assert emp == {
            "metric": "qcew_employment_month3",
            "value": 3520000.0,
            "as_of": "2024-Q3",
            "year": 2024,
            "quarter": 3,
            "ownership": "private",
            "industry_code": "10",
            "geo_level": "msa",
            "msa_code": "19100",
            "area_fips": "C1910",
            "source_id": "BLS-QCEW",
        }

    def test_skips_blank_and_dot_values(self) -> None:
        rows = [
            {
                "own_code": "5",
                "industry_code": "10",
                "month3_emplvl": ".",  # BLS suppression marker
                "avg_wkly_wage": "",
                "total_qtrly_wages": None,
                "qtrly_estabs": "120",
            }
        ]
        records = normalize_records(
            rows,
            msa_code="19100",
            area_fips="C1910",
            year=2024,
            qtr=3,
            source_id="BLS-QCEW",
        )
        assert len(records) == 1
        assert records[0]["metric"] == "qcew_qtrly_estabs"
