"""Unit tests for etl/ingest_census_permits.py.

Network-free: covers the CBSA -> FRED series mappings (monthly SA + annual
NSA county fallback), the supported-metro gates, and the normalize() output
schemas (downstream consumers in research_sandbox/ depend on the monthly
contract; the annual contract is new).
"""

from __future__ import annotations

import pytest
from etl.ingest_census_permits import (
    CBSA_TO_FRED,
    CBSA_TO_FRED_ANNUAL_COUNTIES,
    normalize,
    normalize_annual,
)


class TestCbsaToFredMap:
    def test_capital_deployment_metros_present(self) -> None:
        # Each metro currently configured in agents/configs that FRED carries.
        for cbsa in ("12420", "13820", "19100", "33260", "41700"):
            assert cbsa in CBSA_TO_FRED, f"missing CBSA {cbsa}"

    def test_series_ids_have_expected_suffix(self) -> None:
        # All values are seasonally-adjusted total private permits.
        for series in CBSA_TO_FRED.values():
            assert series.endswith("BPPRIVSA"), series

    def test_smaller_metros_are_absent_from_monthly_map(self) -> None:
        # FRED has no MSA-level monthly series for these — they live in the
        # annual county fallback below.
        for cbsa in ("13140", "26620", "43300"):
            assert cbsa not in CBSA_TO_FRED


class TestCbsaToFredAnnualCounties:
    def test_smaller_metros_present_in_annual_map(self) -> None:
        for cbsa in ("13140", "26620", "43300"):
            assert cbsa in CBSA_TO_FRED_ANNUAL_COUNTIES, f"missing CBSA {cbsa}"

    def test_no_overlap_with_monthly_map(self) -> None:
        # A CBSA in both maps would be ambiguous in main() dispatch.
        assert set(CBSA_TO_FRED) & set(CBSA_TO_FRED_ANNUAL_COUNTIES) == set()

    def test_each_entry_has_series_list(self) -> None:
        for cbsa, spec in CBSA_TO_FRED_ANNUAL_COUNTIES.items():
            assert isinstance(spec.get("series"), list), cbsa
            assert len(spec["series"]) >= 1, cbsa
            for sid in spec["series"]:
                assert sid.startswith("BPPRIV"), f"{cbsa}: {sid}"

    def test_beaumont_has_coverage_note(self) -> None:
        # Newton County is missing on FRED — caller must be told.
        spec = CBSA_TO_FRED_ANNUAL_COUNTIES["13140"]
        assert spec["coverage_note"] is not None
        assert "Newton" in spec["coverage_note"]

    def test_huntsville_full_coverage(self) -> None:
        spec = CBSA_TO_FRED_ANNUAL_COUNTIES["26620"]
        assert spec["coverage_note"] is None
        # Limestone (01083) + Madison (01089).
        assert sorted(spec["series"]) == ["BPPRIV001083", "BPPRIV001089"]


class TestNormalize:
    def test_output_schema_matches_legacy_contract(self) -> None:
        records = normalize(
            [
                {
                    "permit": 1234.0,
                    "year": "2024",
                    "month": "3",
                    "fred_series_id": "DALL148BPPRIVSA",
                },
            ],
            msa_code="19100",
            source_id="CENSUS-BPS-via-FRED",
        )
        assert len(records) == 1
        record = records[0]
        # Legacy callers (run_example_model_overlay.py) expect these exact keys.
        assert set(record) >= {
            "metric",
            "value",
            "as_of",
            "geo_level",
            "msa_code",
            "source_id",
        }
        assert record["metric"] == "permits_total"
        assert record["value"] == 1234
        assert record["as_of"] == "2024-03"
        assert record["geo_level"] == "msa"
        assert record["msa_code"] == "19100"

    def test_value_is_int_typed(self) -> None:
        # FRED returns floats (after seasonal adjustment); legacy schema is int.
        records = normalize(
            [{"permit": 987.4, "year": "2024", "month": "1"}],
            msa_code="12420",
            source_id="x",
        )
        assert isinstance(records[0]["value"], int)
        assert records[0]["value"] == 987

    def test_month_zero_padded(self) -> None:
        records = normalize(
            [{"permit": 1.0, "year": "2024", "month": "9"}],
            msa_code="12420",
            source_id="x",
        )
        assert records[0]["as_of"] == "2024-09"


class TestNormalizeAnnual:
    def test_schema_has_frequency_and_adjustment_flags(self) -> None:
        records = normalize_annual(
            [{"permit": 5449, "year": "2023", "fred_series_ids": "BPPRIV001083,BPPRIV001089"}],
            msa_code="26620",
            source_id="CENSUS-BPS-via-FRED",
            coverage_note=None,
        )
        assert len(records) == 1
        record = records[0]
        # Schema must mark these as annual NSA so consumers don't merge with monthly.
        assert record["metric"] == "permits_total_annual_nsa"
        assert record["frequency"] == "annual"
        assert record["adjustment"] == "NSA"
        assert record["as_of"] == "2023"
        assert record["value"] == 5449
        assert record["msa_code"] == "26620"
        assert record["fred_series_ids"] == "BPPRIV001083,BPPRIV001089"
        assert record["coverage_note"] is None

    def test_coverage_note_propagates(self) -> None:
        records = normalize_annual(
            [{"permit": 100, "year": "2023", "fred_series_ids": "BPPRIV048199"}],
            msa_code="13140",
            source_id="x",
            coverage_note="Newton County (FIPS 048351) not published on FRED",
        )
        assert "Newton" in records[0]["coverage_note"]


def test_main_dispatches_unsupported_metro_with_exit_zero(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A CBSA not in either map should print a warning and return 0, so
    the multi-metro Makefile loop continues rather than aborting."""
    from etl import ingest_census_permits

    monkeypatch.setattr("sys.argv", ["ingest_census_permits.py", "99999"])
    rc = ingest_census_permits.main()
    assert rc == 0
    captured = capsys.readouterr()
    assert "99999" in captured.err
    assert "No FRED permit series" in captured.err


def test_main_dispatches_to_annual_for_huntsville(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pytest.TempPathFactory,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """When called with a CBSA in CBSA_TO_FRED_ANNUAL_COUNTIES, main() should
    route to the annual fetcher and write to the annual-suffix path."""
    from etl import ingest_census_permits

    out_path = str(tmp_path) + "/permits_annual_nsa_26620.parquet"  # type: ignore[operator]

    def fake_fetch(
        msa_code: str, start_year: int, end_year: int
    ) -> tuple[list[dict[str, object]], str | None]:
        assert msa_code == "26620"
        return (
            [
                {"permit": 1865, "year": "1990", "fred_series_ids": "BPPRIV001083,BPPRIV001089"},
                {"permit": 5449, "year": "2023", "fred_series_ids": "BPPRIV001083,BPPRIV001089"},
            ],
            None,
        )

    monkeypatch.setattr(ingest_census_permits, "fetch_annual_county_permits", fake_fetch)
    monkeypatch.setattr("sys.argv", ["ingest_census_permits.py", "26620", "--output", out_path])
    rc = ingest_census_permits.main()
    assert rc == 0
    captured = capsys.readouterr()
    assert "annual NSA county sum" in captured.out
    # File was written.
    import pandas as pd

    df = pd.read_parquet(out_path)
    assert len(df) == 2
    assert set(df["frequency"]) == {"annual"}
    assert set(df["adjustment"]) == {"NSA"}
