"""Tests for etl/supply_model.py — supply risk scoring and elasticity.

Uses synthetic data to test without requiring paid CoStar parquets.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from etl.supply_model import (
    ElasticityResult,
    SupplyRiskScore,
    build_submarket_panel,
    compute_price_elasticity,
    dedupe_costar_dates,
    infer_forecast_flag,
    parse_costar_date,
    score_supply_risk,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def synthetic_costar_df():
    """Synthetic CoStar parquet-format DataFrame (40 quarters)."""
    periods = pd.period_range("2015Q1", periods=40, freq="Q")
    n = len(periods)
    rng = np.random.default_rng(42)

    dates = [f"{p.year} Q{p.quarter}" for p in periods]
    # Mark last 8 as forecasts
    for i in range(n - 8, n):
        dates[i] = f"{periods[i].year} Q{periods[i].quarter} EST"

    return pd.DataFrame({
        "date": dates,
        "star_rating": "1 & 2 Star",
        "submarket": "Test Submarket",
        "is_forecast": [i >= n - 8 for i in range(n)],
        "vacancy_rate": np.clip(0.06 + rng.normal(0, 0.01, n), 0.02, 0.15),
        "stabilized_vacancy": np.full(n, 0.06),
        "occupancy_rate": np.clip(0.94 + rng.normal(0, 0.01, n), 0.85, 0.98),
        "inventory_units": np.full(n, 10000, dtype=int),
        "under_construction": rng.integers(0, 500, n).astype(float),
        "deliveries": rng.integers(0, 200, n).astype(float),
        "deliveries_12mo": rng.integers(100, 800, n).astype(float),
        "deliveries_gross": rng.integers(100, 800, n),
        "absorption_units_12mo": rng.integers(50, 600, n).astype(float),
        "absorption_pct": rng.uniform(-0.01, 0.02, n),
        "construction_starts_12mo": rng.integers(0, 300, n).astype(float),
        "effective_rent_unit": 1200 + np.cumsum(rng.normal(5, 10, n)),
        "asking_rent_unit": 1250 + np.cumsum(rng.normal(5, 10, n)),
        "effective_rent_growth_yoy": rng.uniform(-0.03, 0.05, n),
        "asking_rent_growth_yoy": rng.uniform(-0.03, 0.05, n),
    })


@pytest.fixture
def synthetic_panel(synthetic_costar_df):
    """Panel built from synthetic data."""
    return build_submarket_panel(synthetic_costar_df, star_rating="1 & 2 Star")


# ---------------------------------------------------------------------------
# Date parsing
# ---------------------------------------------------------------------------

class TestParseCostarDate:
    def test_basic_quarter(self):
        period, is_fc = parse_costar_date("2025 Q3")
        assert str(period) == "2025Q3"
        assert is_fc is False

    def test_est_quarter(self):
        period, is_fc = parse_costar_date("2026 Q1 EST")
        assert str(period) == "2026Q1"
        assert is_fc is True

    def test_qtd_quarter(self):
        period, is_fc = parse_costar_date("2026 Q1 QTD")
        assert str(period) == "2026Q1"
        assert is_fc is False

    def test_invalid_raises(self):
        with pytest.raises(ValueError):
            parse_costar_date("January 2025")


class TestDedupeCostarDates:
    def test_removes_duplicate_quarter(self):
        df = pd.DataFrame({
            "date": ["2026 Q1 QTD", "2026 Q1 EST", "2025 Q4"],
            "value": [100, 200, 300],
        })
        result = dedupe_costar_dates(df)
        assert len(result) == 2  # Q1 deduped, Q4 kept
        # QTD should win over EST
        q1_row = result[result["period_q"] == pd.Period("2026Q1")]
        assert q1_row["value"].iloc[0] == 100

    def test_preserves_single_rows(self):
        df = pd.DataFrame({
            "date": ["2025 Q1", "2025 Q2", "2025 Q3"],
            "value": [1, 2, 3],
        })
        result = dedupe_costar_dates(df)
        assert len(result) == 3


# ---------------------------------------------------------------------------
# Panel construction
# ---------------------------------------------------------------------------

class TestBuildSubmarketPanel:
    def test_returns_period_index(self, synthetic_panel):
        assert isinstance(synthetic_panel.index, pd.PeriodIndex)

    def test_has_required_columns(self, synthetic_panel):
        required = [
            "is_forecast", "vacancy_rate", "stabilized_vacancy", "vacancy_gap",
            "inventory_units", "under_construction", "deliveries_12mo",
            "absorption_units_12mo", "effective_rent_growth_yoy",
            "uc_share", "deliveries_12mo_share", "absorption_12mo_share",
        ]
        for col in required:
            assert col in synthetic_panel.columns, f"Missing column: {col}"

    def test_filters_star_rating(self, synthetic_costar_df):
        # Add a second star rating
        extra = synthetic_costar_df.copy()
        extra["star_rating"] = "3 Star"
        combined = pd.concat([synthetic_costar_df, extra])

        panel = build_submarket_panel(combined, star_rating="1 & 2 Star")
        assert all(panel["star_rating"] == "1 & 2 Star")

    def test_uc_share_bounded(self, synthetic_panel):
        valid = synthetic_panel["uc_share"].dropna()
        assert valid.min() >= 0
        assert valid.max() <= 1.0  # UC shouldn't exceed inventory

    def test_forecast_flag_set(self, synthetic_panel):
        assert synthetic_panel["is_forecast"].any()
        assert (~synthetic_panel["is_forecast"]).any()


# ---------------------------------------------------------------------------
# Supply risk scoring
# ---------------------------------------------------------------------------

class TestScoreSupplyRisk:
    def test_returns_supply_risk_score(self, synthetic_panel):
        risk = score_supply_risk(synthetic_panel)
        assert isinstance(risk, SupplyRiskScore)
        assert 0 <= risk.total_score <= 100
        assert risk.verdict in ("PROCEED", "CAUTION", "PASS")

    def test_has_four_dimensions(self, synthetic_panel):
        risk = score_supply_risk(synthetic_panel)
        assert len(risk.dimensions) == 4

    def test_dimension_scores_bounded(self, synthetic_panel):
        risk = score_supply_risk(synthetic_panel)
        for d in risk.dimensions:
            assert 0 <= d.score <= d.max_points
            assert d.signal in ("LOW", "MODERATE", "HIGH", "UNKNOWN")

    def test_high_pipeline_scores_low(self):
        """A submarket with massive UC should score poorly on pipeline depth."""
        df = pd.DataFrame({
            "date": [f"202{y} Q{q}" for y in range(0, 5) for q in range(1, 5)],
            "star_rating": "1 & 2 Star",
            "submarket": "Test",
            "is_forecast": [i >= 16 for i in range(20)],
            "vacancy_rate": [0.08] * 20,
            "stabilized_vacancy": [0.06] * 20,
            "occupancy_rate": [0.92] * 20,
            "inventory_units": [10000] * 20,
            "under_construction": [2000.0] * 20,  # 20% UC — very high
            "deliveries": [500.0] * 20,
            "deliveries_12mo": [2000.0] * 20,
            "deliveries_gross": [2000] * 20,
            "absorption_units_12mo": [500.0] * 20,  # Can't absorb deliveries
            "absorption_pct": [0.01] * 20,
            "construction_starts_12mo": [1000.0] * 20,
            "effective_rent_unit": [1200.0] * 20,
            "asking_rent_unit": [1250.0] * 20,
            "effective_rent_growth_yoy": [-0.04] * 20,  # Negative rent growth
            "asking_rent_growth_yoy": [-0.04] * 20,
        })
        panel = build_submarket_panel(df, star_rating="1 & 2 Star")
        risk = score_supply_risk(panel)

        # Pipeline depth should be HIGH risk (0 points)
        pipeline_dim = [d for d in risk.dimensions if d.name == "Pipeline Depth"][0]
        assert pipeline_dim.signal == "HIGH"
        assert pipeline_dim.score == 0

        # Overall should not be PROCEED
        assert risk.verdict != "PROCEED"

    def test_empty_panel(self):
        risk = score_supply_risk(pd.DataFrame(columns=["is_forecast"]))
        assert risk.verdict == "UNKNOWN"

    def test_healthy_market_scores_high(self):
        """Low UC, positive absorption, rent growth → should score well."""
        df = pd.DataFrame({
            "date": [f"202{y} Q{q}" for y in range(0, 5) for q in range(1, 5)],
            "star_rating": "1 & 2 Star",
            "submarket": "Healthy Market",
            "is_forecast": [i >= 16 for i in range(20)],
            "vacancy_rate": [0.04] * 20,
            "stabilized_vacancy": [0.05] * 20,
            "occupancy_rate": [0.96] * 20,
            "inventory_units": [10000] * 20,
            "under_construction": [200.0] * 20,  # Only 2% UC
            "deliveries": [50.0] * 20,
            "deliveries_12mo": [200.0] * 20,
            "deliveries_gross": [200] * 20,
            "absorption_units_12mo": [400.0] * 20,  # 2x absorption
            "absorption_pct": [0.02] * 20,
            "construction_starts_12mo": [100.0] * 20,
            "effective_rent_unit": [1500.0] * 20,
            "asking_rent_unit": [1550.0] * 20,
            "effective_rent_growth_yoy": [0.03] * 20,  # 3% growth
            "asking_rent_growth_yoy": [0.03] * 20,
        })
        panel = build_submarket_panel(df, star_rating="1 & 2 Star")
        risk = score_supply_risk(panel)

        assert risk.total_score >= 70
        assert risk.verdict == "PROCEED"


# ---------------------------------------------------------------------------
# Price elasticity
# ---------------------------------------------------------------------------

class TestComputePriceElasticity:
    def test_returns_elasticity_result(self, synthetic_panel):
        result = compute_price_elasticity(synthetic_panel)
        assert isinstance(result, ElasticityResult)

    def test_has_coefficients(self, synthetic_panel):
        result = compute_price_elasticity(synthetic_panel)
        assert "intercept" in result.coefficients
        assert len(result.coefficients) >= 2

    def test_r_squared_bounded(self, synthetic_panel):
        result = compute_price_elasticity(synthetic_panel)
        # R² can be negative with bad fits but should be in [-1, 1] range
        assert result.r_squared <= 1.0

    def test_low_confidence_with_few_observations(self):
        """Panel with < 8 actuals should flag low confidence."""
        df = pd.DataFrame({
            "date": ["2024 Q1", "2024 Q2", "2024 Q3"],
            "star_rating": "1 & 2 Star",
            "submarket": "Tiny",
            "is_forecast": [False, False, False],
            "vacancy_rate": [0.05, 0.06, 0.07],
            "stabilized_vacancy": [0.05, 0.05, 0.05],
            "occupancy_rate": [0.95, 0.94, 0.93],
            "inventory_units": [1000, 1000, 1000],
            "under_construction": [0.0, 0.0, 0.0],
            "deliveries": [0.0, 0.0, 0.0],
            "deliveries_12mo": [0.0, 0.0, 0.0],
            "deliveries_gross": [0, 0, 0],
            "absorption_units_12mo": [0.0, 0.0, 0.0],
            "absorption_pct": [0.0, 0.0, 0.0],
            "construction_starts_12mo": [0.0, 0.0, 0.0],
            "effective_rent_unit": [1200.0, 1210.0, 1220.0],
            "asking_rent_unit": [1250.0, 1260.0, 1270.0],
            "effective_rent_growth_yoy": [0.02, 0.01, -0.01],
            "asking_rent_growth_yoy": [0.02, 0.01, -0.01],
        })
        panel = build_submarket_panel(df)
        result = compute_price_elasticity(panel)
        assert result.low_confidence is True
