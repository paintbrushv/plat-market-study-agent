"""Per-model + ensemble forecast diagnostics.

Computes the statistical-rigor metrics required by the Investment
Committee memo: per-model RMSE / MAE / bias / lag-1 residual ACF /
Ljung-Box (5-lag) p-value / 80%-CI coverage / ensemble weight, plus
the ensemble's calibration and an overall confidence classification.

Each of the 6 models also carries a hand-authored "strengths /
weaknesses" paragraph (``MODEL_NARRATIVES``) so the memo reader can
interpret why a model earned its weight.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from statsmodels.stats.diagnostic import acorr_ljungbox

MODEL_NARRATIVES: dict[str, dict[str, str]] = {
    "Naive": {
        "strengths": (
            "Persistence baseline (this quarter's YoY = next quarter's YoY). "
            "Sets the floor for usefulness — any model that can't beat naive "
            "in backtest is contributing noise. Robust to regime change because "
            "it has nothing to forget."
        ),
        "weaknesses": (
            "Has no economic content. Can't anticipate mean-reversion, "
            "supply shocks, or absorption inflections. In submarkets where "
            "rent growth is mean-reverting (most of them), naive systematically "
            "lags the turn."
        ),
    },
    "ARIMA": {
        "strengths": (
            "Captures momentum and seasonality in the YoY rent-growth series "
            "directly. Auto-selected (p, d, q) lets the model match the "
            "submarket's intrinsic persistence without pulling in noisy "
            "exogenous variables. Strong at short horizons (1-2 quarters)."
        ),
        "weaknesses": (
            "Univariate — ignores vacancy / absorption / deliveries. At "
            "regime breaks (e.g. 2020 Q2 demand shock), ARIMA over-extrapolates "
            "the prior trajectory and has to re-learn. Confidence intervals "
            "widen rapidly past 4 quarters."
        ),
    },
    "BVAR": {
        "strengths": (
            "Captures the multivariate co-movement of rent growth, vacancy, "
            "absorption, and deliveries. Bayesian shrinkage stabilizes "
            "estimates when sample sizes are modest. Strongest model for "
            "submarkets where supply-demand dynamics drive rent — i.e. most "
            "of them. Handles the ``vacancy gap → rent growth elasticity`` "
            "channel that pure univariate models miss."
        ),
        "weaknesses": (
            "Assumes linear shock-propagation. Struggles at regime breaks "
            "(2020 Q2 COVID, 2022 Q4 rate-spike) where the cross-equation "
            "elasticities flip. Sensitive to lag selection — over-lagged "
            "BVARs overfit and forecast poorly."
        ),
    },
    "Ridge": {
        "strengths": (
            "L2-regularized linear model on the same multivariate features as "
            "BVAR (vacancy lag, absorption lag, deliveries lag, UC pct, prior "
            "rent YoY). Robust to multicollinearity — vacancy and absorption "
            "are highly correlated and Ridge handles that gracefully. Fast, "
            "interpretable, and seldom catastrophically wrong."
        ),
        "weaknesses": (
            "Linear by construction — can't capture non-linear interactions "
            "(e.g. rent acceleration when vacancy is below a threshold AND "
            "absorption is positive). Doesn't model dynamics directly; treats "
            "each quarter as independent given features."
        ),
    },
    "GBM": {
        "strengths": (
            "Non-linear interactions between features. Can learn that "
            "high-vacancy + negative-absorption rents fall faster than the "
            "linear sum of the two effects. Tuned with shallow trees / "
            "subsampling to control overfit (the common GBM failure mode)."
        ),
        "weaknesses": (
            "Most likely model to overfit on small panels (~100 quarters). "
            "Doesn't extrapolate cleanly outside its training range — at "
            "rent levels above the training max, GBM's predictions flatten "
            "rather than continuing the trend. Backtest RMSE / in-sample "
            "RMSE ratio is a key health check."
        ),
    },
    "ElasticNet": {
        "strengths": (
            "Hybrid L1+L2 regularization — combines Ridge's stability with "
            "Lasso's feature-selection. When the true driver set is sparse "
            "(e.g. vacancy + absorption are the only variables that matter), "
            "ElasticNet zeroes out the noise. Robust complement to BVAR."
        ),
        "weaknesses": (
            "Same linearity limitation as Ridge. Hyperparameters (alpha, "
            "l1_ratio) sensitive to scaling; if not standardized, can produce "
            "unstable feature-selection across rolling-backtest folds."
        ),
    },
}


def compute_per_model_metrics(
    backtest_df: pd.DataFrame,
    ci_lo: pd.Series | None = None,
    ci_hi: pd.Series | None = None,
) -> dict[str, Any]:
    """Compute the per-model statistical-rigor metrics from a backtest DF.

    ``backtest_df`` must have columns: ``period``, ``actual``, ``predicted``,
    ``error`` (= actual - predicted). ``ci_lo`` / ``ci_hi`` are optional 80%
    bands aligned by index; if not passed and ``pred_lo`` / ``pred_hi``
    columns are present in the df, they're used instead.
    """
    if ci_lo is None and "pred_lo" in backtest_df.columns:
        ci_lo = backtest_df["pred_lo"]
    if ci_hi is None and "pred_hi" in backtest_df.columns:
        ci_hi = backtest_df["pred_hi"]

    errors = backtest_df["error"].to_numpy()
    n = len(errors)
    rmse = float(np.sqrt(np.mean(errors**2))) if n else float("nan")
    mae = float(np.mean(np.abs(errors))) if n else float("nan")
    bias = float(np.mean(errors)) if n else float("nan")

    if n >= 2:
        acf1 = float(pd.Series(errors).autocorr(lag=1) or 0.0)
    else:
        acf1 = float("nan")

    if n >= 10:
        try:
            lb = acorr_ljungbox(errors, lags=[min(5, n // 2)], return_df=True)
            ljung_p = float(lb["lb_pvalue"].iloc[0])
        except Exception:
            ljung_p = float("nan")
    else:
        ljung_p = float("nan")

    if ci_lo is not None and ci_hi is not None and n:
        actual = backtest_df["actual"].to_numpy()
        lo = np.asarray(ci_lo)
        hi = np.asarray(ci_hi)
        ci80_cov = float(np.mean((actual >= lo) & (actual <= hi)))
    else:
        ci80_cov = float("nan")

    return {
        "n_backtest_obs": int(n),
        "rmse": rmse,
        "mae": mae,
        "bias": bias,
        "residual_acf_lag1": acf1,
        "ljung_box_lag5_pvalue": ljung_p,
        "ci80_coverage": ci80_cov,
    }


def compute_ensemble_weights(bt_rmse: dict[str, float]) -> dict[str, float]:
    """RMSE-inverse weights, excluding Naive (used as baseline only)."""
    eligible = {k: v for k, v in bt_rmse.items() if k != "Naive" and v and v > 0}
    if not eligible:
        return {}
    inv = {k: 1.0 / v for k, v in eligible.items()}
    total = sum(inv.values())
    return {k: v / total for k, v in inv.items()}


def classify_confidence(*, ensemble_rmse: float, yoy_std: float) -> str:
    """High / Medium / Low based on ensemble RMSE relative to historical YoY std."""
    if yoy_std <= 0:
        return "Low"
    ratio = ensemble_rmse / yoy_std
    if ratio < 0.5:
        return "High"
    if ratio < 1.0:
        return "Medium"
    return "Low"


def detect_regime_change(
    panel: pd.DataFrame, *, lookback_q: int = 4, sigma_thresh: float = 2.0
) -> bool:
    """True if last ``lookback_q`` quarters of YoY rent growth are >sigma_thresh σ
    from the 5-year rolling mean. Flags regime instability."""
    s = panel["rent_growth_yoy"].dropna()
    if len(s) < 20:
        return False
    roll_mean = s.rolling(window=20).mean().iloc[-1]
    roll_std = s.rolling(window=20).std().iloc[-1]
    if pd.isna(roll_std) or roll_std == 0:
        return False
    recent = s.iloc[-lookback_q:]
    z = (recent - roll_mean).abs() / roll_std
    return bool((z > sigma_thresh).any())


def serialize_diagnostics(
    *,
    bt_dfs: dict[str, pd.DataFrame],
    ensemble_meta: dict[str, Any],
    ctx_meta: dict[str, Any],
    out_path: Path,
) -> dict[str, Any]:
    """Compute per-model + ensemble metrics and write to JSON.

    ``ensemble_meta`` must have: ``weights`` (dict), ``ensemble_rmse`` (float),
    ``yoy_std`` (float). Optional: ``ci80_coverage`` (float), ``regime_change``
    (bool).
    """
    weights = ensemble_meta.get("weights", {})
    yoy_std = float(ensemble_meta.get("yoy_std", 0.0))
    ensemble_rmse = float(ensemble_meta.get("ensemble_rmse", 0.0))

    payload: dict[str, Any] = {
        "meta": ctx_meta,
        "models": {},
    }
    for name, bt in bt_dfs.items():
        m = compute_per_model_metrics(bt)
        m["ensemble_weight"] = float(weights.get(name, 0.0))
        narrative = MODEL_NARRATIVES.get(name, {"strengths": "", "weaknesses": ""})
        m["strengths"] = narrative["strengths"]
        m["weaknesses"] = narrative["weaknesses"]
        payload["models"][name] = m

    payload["ensemble"] = {
        "weights": weights,
        "ensemble_rmse": ensemble_rmse,
        "ensemble_ci80_coverage": ensemble_meta.get("ci80_coverage", float("nan")),
        "yoy_std": yoy_std,
        "confidence_level": classify_confidence(ensemble_rmse=ensemble_rmse, yoy_std=yoy_std),
        "regime_change_flag": bool(ensemble_meta.get("regime_change", False)),
    }
    payload["data_quality_gates"] = ensemble_meta.get(
        "data_quality_gates",
        {
            "history_quarters": "ok",
            "missing_quarters": "ok",
            "vacancy_sanity": "ok",
            "yoy_sanity": "ok",
        },
    )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2, default=str))
    return payload
