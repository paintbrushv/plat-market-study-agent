import json
from pathlib import Path

import pandas as pd


def test_render_memo_includes_required_sections(tmp_path: Path) -> None:
    from etl.render_forecast_memo import render_memo

    diag = {
        "meta": {
            "metro_slug": "austin_tx",
            "property": "Example Garden",
            "tier": "1 & 2 Star",
            "tier_slug": "1_2_star",
            "panel_quarters": 100,
            "panel_start": "2000Q1",
            "panel_end": "2026Q1",
            "forecast_horizon": 8,
        },
        "models": {
            m: {
                "n_backtest_obs": 60,
                "rmse": 0.02,
                "mae": 0.015,
                "bias": 0.001,
                "residual_acf_lag1": 0.1,
                "ljung_box_lag5_pvalue": 0.4,
                "ci80_coverage": 0.78,
                "ensemble_weight": 0.20 if m != "Naive" else 0.0,
                "strengths": "S",
                "weaknesses": "W",
            }
            for m in ("Naive", "ARIMA", "BVAR", "Ridge", "GBM", "ElasticNet")
        },
        "ensemble": {
            "weights": {"ARIMA": 0.20, "BVAR": 0.25, "Ridge": 0.20, "GBM": 0.15, "ElasticNet": 0.20},  # noqa: E501
            "ensemble_rmse": 0.018,
            "ensemble_ci80_coverage": 0.81,
            "yoy_std": 0.04,
            "confidence_level": "Medium",
            "regime_change_flag": False,
        },
        "data_quality_gates": {"history_quarters": "ok", "missing_quarters": "ok"},
    }
    diag_path = tmp_path / "diag.json"
    diag_path.write_text(json.dumps(diag))

    ens = pd.DataFrame({
        "period": ["2026Q2", "2026Q3", "2026Q4", "2027Q1", "2027Q2", "2027Q3", "2027Q4", "2028Q1"],
        "base": [-0.005, 0.0, 0.005, 0.01, 0.015, 0.018, 0.02, 0.022],
        "bull": [0.01, 0.018, 0.025, 0.030, 0.035, 0.038, 0.042, 0.045],
        "bear": [-0.025, -0.022, -0.018, -0.012, -0.008, -0.005, 0.0, 0.005],
        "ci_80_lo": [-0.02, -0.018, -0.015, -0.010, -0.005, 0.0, 0.005, 0.010],
        "ci_80_hi": [0.010, 0.018, 0.025, 0.030, 0.035, 0.040, 0.045, 0.050],
        "ci_95_lo": [-0.030, -0.028, -0.025, -0.022, -0.018, -0.015, -0.010, -0.005],
        "ci_95_hi": [0.025, 0.030, 0.035, 0.040, 0.045, 0.050, 0.055, 0.060],
    })
    ens_path = tmp_path / "ens.csv"
    ens.to_csv(ens_path, index=False)

    out_md = tmp_path / "memo.md"
    render_memo(
        diagnostics_json=diag_path,
        ensemble_csv=ens_path,
        property_config="agents/configs/austin_tx.yaml",
        out_md=out_md,
    )
    assert out_md.exists()
    text = out_md.read_text()
    for section in (
        "# Market Forecast",
        "## Executive Summary",
        "## 1. Vacancy Rate Forecast",
        "## 2. Rent Growth Forecast",
        "## 3. Supply Pipeline",
        "## 4. Demand Drivers",
        "## 5. Scenario Analysis",
        "## 6. Investment Implications",
        "## 7. Methodology",
        "## Model Diagnostics",
    ):
        assert section in text, f"Missing required section: {section!r}"
    for m in ("Naive", "ARIMA", "BVAR", "Ridge", "GBM", "ElasticNet"):
        assert f"| {m} |" in text or f"| **{m}** |" in text
    assert "Medium" in text
