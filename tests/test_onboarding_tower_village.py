"""Regression test: compare test-onboarding output to production Tower Village data.

Run the /full-onboarding skill against the Tower Village raw rent roll with
``--test-dir test-onboarding --skip-commit --skip-comps``, then execute:

    uv run pytest tests/test_onboarding_tower_village.py -v

The tests compare the test output against the known production values for
Tower Village Apartments (264 units, Irving TX).
"""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Production (ground truth)
PROD_DIR = PROJECT_ROOT / "reports" / "dallas-tx" / "tower-village" / "rent-roll" / "clean"
PROD_RR = PROD_DIR / "rent_roll_standardized.csv"
PROD_FP = PROD_DIR / "floorplan_summary.csv"
PROD_ANALYSIS_MD = PROD_DIR / "rent_roll_analysis_detailed.md"
PROD_ANALYSIS_HTML = PROD_DIR / "rent_roll_analysis.html"

# Test output
TEST_DIR = PROJECT_ROOT / "test-onboarding"
TEST_RR = TEST_DIR / "rent-roll" / "clean" / "rent_roll_standardized.csv"
TEST_FP = TEST_DIR / "rent-roll" / "clean" / "floorplan_summary.csv"
TEST_ANALYSIS_MD = TEST_DIR / "rent-roll" / "clean" / "rent_roll_analysis_detailed.md"
TEST_ANALYSIS_HTML = TEST_DIR / "rent-roll" / "clean" / "rent_roll_analysis.html"

# Expected values from production run
EXPECTED_UNITS = 264
EXPECTED_BED_TYPES = {"1BR", "2BR", "3BR"}
EXPECTED_FLOORPLAN_COUNT = 31


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_csv(path: Path) -> list[dict[str, str]]:
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _load_floorplans(path: Path) -> list[dict[str, str]]:
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _occupancy(rows: list[dict[str, str]]) -> float:
    total = len(rows)
    occupied = sum(1 for r in rows if r["status"] == "Occupied")
    return occupied / total * 100 if total else 0.0


def _avg_market_rent(rows: list[dict[str, str]]) -> float:
    rents = [float(r["market_rent"]) for r in rows if r.get("market_rent")]
    return sum(rents) / len(rents) if rents else 0.0


def _unit_count_by_bed_type(rows: list[dict[str, str]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for r in rows:
        bt = r["bed_type"]
        counts[bt] = counts.get(bt, 0) + 1
    return counts


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def prod_rr() -> list[dict[str, str]]:
    if not PROD_RR.exists():
        pytest.skip("Production rent roll not found")
    return _load_csv(PROD_RR)


@pytest.fixture()
def test_rr() -> list[dict[str, str]]:
    if not TEST_RR.exists():
        pytest.skip(
            "Test rent roll not found. Run /full-onboarding with "
            "--test-dir test-onboarding --skip-commit --skip-comps first."
        )
    return _load_csv(TEST_RR)


@pytest.fixture()
def prod_fp() -> list[dict[str, str]]:
    if not PROD_FP.exists():
        pytest.skip("Production floorplan summary not found")
    return _load_floorplans(PROD_FP)


@pytest.fixture()
def test_fp() -> list[dict[str, str]]:
    if not TEST_FP.exists():
        pytest.skip(
            "Test floorplan summary not found. Run /full-onboarding with "
            "--test-dir test-onboarding --skip-commit --skip-comps first."
        )
    return _load_floorplans(TEST_FP)


# ---------------------------------------------------------------------------
# Tests: Unit Counts
# ---------------------------------------------------------------------------


class TestUnitCounts:
    """Verify unit counts match between test output and production."""

    def test_total_units_match_expected(self, test_rr: list[dict[str, str]]) -> None:
        assert len(test_rr) == EXPECTED_UNITS

    def test_total_units_match_production(
        self, prod_rr: list[dict[str, str]], test_rr: list[dict[str, str]]
    ) -> None:
        assert len(test_rr) == len(prod_rr)

    def test_bed_types_present(self, test_rr: list[dict[str, str]]) -> None:
        bed_types = {r["bed_type"] for r in test_rr}
        assert bed_types == EXPECTED_BED_TYPES

    def test_bed_type_counts_match_production(
        self, prod_rr: list[dict[str, str]], test_rr: list[dict[str, str]]
    ) -> None:
        prod_counts = _unit_count_by_bed_type(prod_rr)
        test_counts = _unit_count_by_bed_type(test_rr)
        assert test_counts == prod_counts


# ---------------------------------------------------------------------------
# Tests: Market Rents
# ---------------------------------------------------------------------------


class TestMarketRents:
    """Verify market rents are consistent between test and production."""

    def test_avg_market_rent_within_tolerance(
        self, prod_rr: list[dict[str, str]], test_rr: list[dict[str, str]]
    ) -> None:
        """Average market rent should be within $5 of production."""
        prod_avg = _avg_market_rent(prod_rr)
        test_avg = _avg_market_rent(test_rr)
        assert abs(test_avg - prod_avg) <= 5.0, (
            f"Avg market rent diverged: test=${test_avg:.2f} vs prod=${prod_avg:.2f}"
        )

    def test_no_zero_market_rent_occupied(self, test_rr: list[dict[str, str]]) -> None:
        """Occupied units must not have $0 market rent."""
        zero_rent = [
            r["unit_id"]
            for r in test_rr
            if r["status"] == "Occupied"
            and r.get("market_rent")
            and float(r["market_rent"]) == 0
        ]
        assert len(zero_rent) == 0, f"Occupied units with $0 market rent: {zero_rent}"

    def test_all_units_have_market_rent(self, test_rr: list[dict[str, str]]) -> None:
        """Every unit should have a non-empty market_rent value."""
        missing = [r["unit_id"] for r in test_rr if not r.get("market_rent")]
        assert len(missing) == 0, f"Units missing market_rent: {missing}"


# ---------------------------------------------------------------------------
# Tests: Occupancy
# ---------------------------------------------------------------------------


class TestOccupancy:
    """Verify occupancy is reasonable and matches production."""

    def test_occupancy_within_range(self, test_rr: list[dict[str, str]]) -> None:
        """Occupancy should be between 50% and 100%."""
        occ = _occupancy(test_rr)
        assert 50.0 <= occ <= 100.0, f"Occupancy out of range: {occ:.1f}%"

    def test_occupancy_matches_production(
        self, prod_rr: list[dict[str, str]], test_rr: list[dict[str, str]]
    ) -> None:
        """Occupancy should match production within 1 percentage point."""
        prod_occ = _occupancy(prod_rr)
        test_occ = _occupancy(test_rr)
        assert abs(test_occ - prod_occ) <= 1.0, (
            f"Occupancy diverged: test={test_occ:.1f}% vs prod={prod_occ:.1f}%"
        )

    def test_valid_status_values(self, test_rr: list[dict[str, str]]) -> None:
        """All status values should be Occupied, Vacant, or Notice."""
        valid = {"Occupied", "Vacant", "Notice"}
        statuses = {r["status"] for r in test_rr}
        invalid = statuses - valid
        assert not invalid, f"Invalid status values: {invalid}"


# ---------------------------------------------------------------------------
# Tests: Floorplan Summary
# ---------------------------------------------------------------------------


class TestFloorplanSummary:
    """Verify floorplan summary matches production."""

    def test_floorplan_count_matches_production(
        self, prod_fp: list[dict[str, str]], test_fp: list[dict[str, str]]
    ) -> None:
        assert len(test_fp) == len(prod_fp)

    def test_floorplan_unit_totals_match_rent_roll(
        self, test_rr: list[dict[str, str]], test_fp: list[dict[str, str]]
    ) -> None:
        """Sum of units in floorplan summary should match rent roll count."""
        fp_total = sum(int(r["Units"]) for r in test_fp)
        assert fp_total == len(test_rr), (
            f"Floorplan total ({fp_total}) != rent roll count ({len(test_rr)})"
        )

    def test_floorplan_codes_match_production(
        self, prod_fp: list[dict[str, str]], test_fp: list[dict[str, str]]
    ) -> None:
        """Same set of floorplan codes should appear."""
        prod_codes = {r["PlanCode"] for r in prod_fp}
        test_codes = {r["PlanCode"] for r in test_fp}
        assert test_codes == prod_codes

    def test_floorplan_avg_rents_within_tolerance(
        self, prod_fp: list[dict[str, str]], test_fp: list[dict[str, str]]
    ) -> None:
        """Average market rents per floorplan should be within $5 of production."""
        prod_rents = {r["PlanCode"]: float(r["AvgMarketRent"]) for r in prod_fp}
        test_rents = {r["PlanCode"]: float(r["AvgMarketRent"]) for r in test_fp}
        for code in prod_rents:
            assert code in test_rents, f"Missing floorplan: {code}"
            diff = abs(test_rents[code] - prod_rents[code])
            assert diff <= 5.0, (
                f"Floorplan {code} rent diverged: "
                f"test=${test_rents[code]:.2f} vs prod=${prod_rents[code]:.2f}"
            )


# ---------------------------------------------------------------------------
# Tests: Critical Columns
# ---------------------------------------------------------------------------


class TestDataQuality:
    """Verify no NaN/empty values in critical columns."""

    CRITICAL_COLUMNS = ["unit_id", "floorplan_code", "bed_type", "sqft", "market_rent"]

    @pytest.mark.parametrize("column", CRITICAL_COLUMNS)
    def test_no_missing_values(
        self, test_rr: list[dict[str, str]], column: str
    ) -> None:
        missing = [
            r.get("unit_id", "?")
            for r in test_rr
            if not r.get(column) or r[column].strip() == ""
        ]
        assert len(missing) == 0, (
            f"Column '{column}' has {len(missing)} missing values"
        )


# ---------------------------------------------------------------------------
# Tests: Output Files Exist
# ---------------------------------------------------------------------------


class TestOutputFiles:
    """Verify all expected output files were generated."""

    def test_rent_roll_csv_exists(self) -> None:
        if not TEST_RR.exists():
            pytest.skip("Test output not generated yet")
        assert TEST_RR.stat().st_size > 0

    def test_floorplan_csv_exists(self) -> None:
        if not TEST_FP.exists():
            pytest.skip("Test output not generated yet")
        assert TEST_FP.stat().st_size > 0

    def test_analysis_markdown_exists(self) -> None:
        if not TEST_ANALYSIS_MD.exists():
            pytest.skip("Test output not generated yet")
        assert TEST_ANALYSIS_MD.stat().st_size > 1000, (
            "Analysis markdown suspiciously small"
        )

    def test_analysis_html_exists(self) -> None:
        if not TEST_ANALYSIS_HTML.exists():
            pytest.skip("Test output not generated yet")
        assert TEST_ANALYSIS_HTML.stat().st_size > 5000, (
            "HTML report too small (< 5KB) - likely broken"
        )

    def test_snapshot_directory_exists(self) -> None:
        snapshot_dir = TEST_DIR / "rent-roll" / "clean" / "snapshots"
        if not snapshot_dir.exists():
            pytest.skip("Test output not generated yet")
        snapshots = list(snapshot_dir.iterdir())
        assert len(snapshots) > 0, "No snapshot directories created"
