import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest


def _make_backtest_df(errors: list[float]) -> pd.DataFrame:
    return pd.DataFrame({
        "period": [pd.Period(f"2020Q{i % 4 + 1}", freq="Q-DEC") for i in range(len(errors))],
        "actual": [0.02] * len(errors),
        "predicted": [0.02 - e for e in errors],
        "error": errors,
    })


def test_per_model_metrics_basic() -> None:
    """Per-model diagnostics: RMSE, MAE, bias, ACF, Ljung-Box, CI coverage, weight."""
    from etl.forecast_diagnostics import compute_per_model_metrics

    bt = _make_backtest_df([0.01, -0.01, 0.02, -0.02, 0.005, -0.005, 0.0, 0.0, 0.01, -0.01])
    metrics = compute_per_model_metrics(
        bt, ci_lo=bt["predicted"] - 0.02, ci_hi=bt["predicted"] + 0.02
    )
    assert metrics["n_backtest_obs"] == 10
    assert metrics["rmse"] == pytest.approx(np.sqrt(np.mean(np.array(bt["error"]) ** 2)), rel=1e-6)
    assert metrics["mae"] == pytest.approx(np.mean(np.abs(bt["error"])), rel=1e-6)
    assert metrics["bias"] == pytest.approx(0.0, abs=1e-3)
    assert -1 <= metrics["residual_acf_lag1"] <= 1
    assert 0 <= metrics["ljung_box_lag5_pvalue"] <= 1
    assert 0 <= metrics["ci80_coverage"] <= 1


def test_ensemble_weight_normalization() -> None:
    """Ensemble weights sum to 1.0 across non-Naive models."""
    from etl.forecast_diagnostics import compute_ensemble_weights

    bt_rmse = {
        "Naive": 0.04, "ARIMA": 0.03, "BVAR": 0.025,
        "Ridge": 0.028, "GBM": 0.035, "ElasticNet": 0.03,
    }
    weights = compute_ensemble_weights(bt_rmse)
    assert "Naive" not in weights
    assert sum(weights.values()) == pytest.approx(1.0, abs=1e-6)
    assert max(weights.items(), key=lambda x: x[1])[0] == "BVAR"


def test_strengths_weaknesses_strings_present() -> None:
    """Every model has a hand-authored strength/weakness paragraph."""
    from etl.forecast_diagnostics import MODEL_NARRATIVES

    for model in ("Naive", "ARIMA", "BVAR", "Ridge", "GBM", "ElasticNet"):
        assert model in MODEL_NARRATIVES
        n = MODEL_NARRATIVES[model]
        assert "strengths" in n and "weaknesses" in n
        assert len(n["strengths"]) > 50
        assert len(n["weaknesses"]) > 50


def test_confidence_level_classification() -> None:
    """Confidence: High if RMSE < 0.5σ, Medium if < 1σ, Low otherwise."""
    from etl.forecast_diagnostics import classify_confidence

    yoy_std = 0.04
    assert classify_confidence(ensemble_rmse=0.015, yoy_std=yoy_std) == "High"
    assert classify_confidence(ensemble_rmse=0.030, yoy_std=yoy_std) == "Medium"
    assert classify_confidence(ensemble_rmse=0.050, yoy_std=yoy_std) == "Low"


def test_serialize_diagnostics_to_json(tmp_path: Path) -> None:
    """Top-level serializer writes a valid JSON file with the required schema."""
    from etl.forecast_diagnostics import serialize_diagnostics

    bt_dfs = {
        "Naive": _make_backtest_df([0.04, -0.04, 0.04, -0.04] * 10),
        "ARIMA": _make_backtest_df([0.02, -0.02] * 20),
        "BVAR": _make_backtest_df([0.015, -0.015] * 20),
        "Ridge": _make_backtest_df([0.02, -0.02] * 20),
        "GBM": _make_backtest_df([0.025, -0.025] * 20),
        "ElasticNet": _make_backtest_df([0.02, -0.02] * 20),
    }
    ensemble_meta = {
        "weights": {"ARIMA": 0.20, "BVAR": 0.25, "Ridge": 0.20, "GBM": 0.15, "ElasticNet": 0.20},
        "ensemble_rmse": 0.018,
        "yoy_std": 0.04,
    }
    out = tmp_path / "model_diagnostics_1_2_star.json"
    serialize_diagnostics(
        bt_dfs=bt_dfs,
        ensemble_meta=ensemble_meta,
        ctx_meta={"property": "Devon", "submarket": "Irving", "tier": "1 & 2 Star"},
        out_path=out,
    )
    assert out.exists()
    payload = json.loads(out.read_text())
    assert payload["meta"]["property"] == "Devon"
    assert "Naive" in payload["models"]
    for m in ("Naive", "ARIMA", "BVAR", "Ridge", "GBM", "ElasticNet"):
        assert payload["models"][m]["rmse"] >= 0
        assert "strengths" in payload["models"][m]
    assert payload["ensemble"]["confidence_level"] in ("High", "Medium", "Low")


def test_per_model_metrics_uses_ci_for_coverage() -> None:
    """When ci_lo/ci_hi columns are present in the backtest df, ci80_coverage is computed."""
    from etl.forecast_diagnostics import compute_per_model_metrics
    bt = pd.DataFrame({
        "period": [pd.Period(f"2020Q{i % 4 + 1}", freq="Q-DEC") for i in range(20)],
        "actual": [0.02] * 20,
        "predicted": [0.02 + 0.01 * (-1) ** i for i in range(20)],
        "error": [-0.01 * (-1) ** i for i in range(20)],
    })
    metrics_tight = compute_per_model_metrics(
        bt, ci_lo=bt["predicted"] - 0.005, ci_hi=bt["predicted"] + 0.005
    )
    assert metrics_tight["ci80_coverage"] == 0.0  # all errors exceed band
    metrics_wide = compute_per_model_metrics(
        bt, ci_lo=bt["predicted"] - 0.05, ci_hi=bt["predicted"] + 0.05
    )
    assert metrics_wide["ci80_coverage"] == 1.0
