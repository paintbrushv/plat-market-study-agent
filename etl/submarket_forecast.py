"""Submarket Rent-Growth Forecast — Generic Econometric & ML Ensemble.

Renamed from ``birmingham_forecast.py``; generalized to run on any
submarket × star-rating tier via a property config YAML.

Usage:
    uv run python -m etl.submarket_forecast --config <path-to-config.yaml> \\
        [--star-rating "1 & 2 Star"] [--horizons 4,12,20]

Architecture (high level):
    1. ``ForecastContext`` — config-derived runtime context (parquet path,
       tier, output dir, horizon).
    2. ``load_panel(ctx)`` — quarterly panel + CoStar forecast frame; runs
       data-quality gates (min history, missing quarters, sanity bounds).
    3. Per-model classes (Naive, ARIMA, BVAR, Ridge, GBM, ElasticNet) — each
       implements ``.fit(panel)`` / ``.predict(horizon)`` / ``.residuals()``.
    4. ``rolling_backtest`` — out-of-sample errors for one model.
    5. ``run_all_backtests`` / ``fit_all_models`` — fan out across the suite.
    6. ``build_ensemble`` — RMSE-inverse-weighted point + 80%/95% CI bands +
       Bull/Bear scenario bands calibrated to backtest RMSE.

Diagnostics (RMSE/MAE/bias/residual ACF/Ljung-Box/CI calibration/ensemble
weight) live in :mod:`etl.forecast_diagnostics` and are computed by the
CLI entry point after ``build_ensemble``.
"""

from __future__ import annotations

import re
import warnings
from dataclasses import dataclass
from pathlib import Path

from etl.forecast_diagnostics import detect_regime_change, serialize_diagnostics

import numpy as np
import pandas as pd
import yaml
from scipy import stats as sp_stats
from sklearn.linear_model import ElasticNet, Ridge
from statsmodels.stats.diagnostic import acorr_ljungbox
from statsmodels.tsa.api import VAR
from statsmodels.tsa.arima.model import ARIMA
from statsmodels.tsa.stattools import adfuller, grangercausalitytests, kpss

warnings.filterwarnings("ignore", category=FutureWarning)
warnings.filterwarnings("ignore", category=UserWarning)

try:
    import lightgbm as lgb

    HAS_LGB = True
except ImportError:
    from sklearn.ensemble import GradientBoostingRegressor

    HAS_LGB = False

ROOT = Path(__file__).resolve().parent.parent

METRO_ID = "birmingham_al"

ENDOG_VARS = [
    "rent_growth_yoy",
    "vacancy_rate",
    "deliveries_pct_inventory",
    "absorption_pct_inventory",
]


# ═══════════════════════════════════════════════════════════════════════
# FORECAST CONTEXT — config-driven dataclass
# ═══════════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class ForecastContext:
    metro_slug: str
    property_name: str
    parquet_path: Path
    star_rating: str
    tier_slug: str
    output_dir: Path
    vintage: str = "2026-Q1"
    forecast_horizon: int = 8
    backtest_min_train: int = 40

    @staticmethod
    def normalize_tier_slug(star_rating: str) -> str:
        s = star_rating.lower()
        s = s.replace("&", "")
        s = re.sub(r"\s+", "_", s.strip())
        s = re.sub(r"_+", "_", s)
        return s.strip("_")


def context_from_config(config_path: Path, star_rating: str | None = None) -> ForecastContext:
    """Build a ``ForecastContext`` from a property config YAML.

    ``output_dir`` is derived as ``report_path.parent / "forecasts"``. If
    ``outputs.report_path`` in the config is relative, ``output_dir`` will
    also be relative — callers must resolve against the project root before
    writing files (``ctx.output_dir.mkdir(parents=True, exist_ok=True)`` at
    pipeline entry suffices).

    ``star_rating`` defaults to ``costar_submarket.primary_star_rating``
    from the config; pass an explicit value to override.
    """
    with open(config_path, encoding="utf-8") as f:
        config = yaml.safe_load(f)
    notes = config["notes"]
    metro_slug = notes["metro_slug"]
    property_name = notes["subject_property"]["name"]
    costar = config["costar_submarket"]
    if star_rating is None:
        star_rating = costar["primary_star_rating"]
    parquet_path = Path(costar["parquet"])
    # output_dir derives from the report_path's parent directory
    report_path = Path(config["outputs"]["report_path"])
    output_dir = report_path.parent / "forecasts"
    return ForecastContext(
        metro_slug=metro_slug,
        property_name=property_name,
        parquet_path=parquet_path,
        star_rating=star_rating,
        tier_slug=ForecastContext.normalize_tier_slug(star_rating),
        output_dir=output_dir,
    )


# ═══════════════════════════════════════════════════════════════════════
# 1. DATA LOADING
# ═══════════════════════════════════════════════════════════════════════


def _parse_period(date_str: str) -> pd.Period | None:
    parts = str(date_str).strip().split()
    if len(parts) >= 2 and parts[0].isdigit():
        try:
            return pd.Period(f"{parts[0]}{parts[1]}", freq="Q-DEC")
        except Exception:
            return None
    return None


def load_panel(ctx: ForecastContext) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Load CoStar parquet, build quarterly panel for the configured star rating.

    Returns (historical_panel, costar_forecasts).
    """
    raw = pd.read_parquet(ctx.parquet_path)
    raw["period"] = raw["date"].apply(_parse_period)
    raw = raw.dropna(subset=["period"])
    raw["period"] = raw["period"].astype("period[Q-DEC]")

    # ── Historical ──
    # Some parquets carry an `is_forecast` column; others rely on EST/QTD suffixes
    # in the date string. Prefer is_forecast when present, fall back to date string.
    has_is_forecast = "is_forecast" in raw.columns
    if has_is_forecast:
        mask_hist = (raw["is_forecast"] == False) & (raw["star_rating"] == ctx.star_rating)  # noqa: E712
    else:
        mask_hist = (raw["star_rating"] == ctx.star_rating) & (
            ~raw["date"].astype(str).str.contains("EST", na=False)
        )
    hist = raw[mask_hist].copy()
    hist = hist[~hist["date"].astype(str).str.contains("EST|QTD", na=False)]
    hist = hist.sort_values("period").drop_duplicates(subset=["period"], keep="last")
    hist = hist.set_index("period")

    hist["deliveries_pct_inventory"] = (
        hist["deliveries_12mo"] / hist["inventory_units"]
    ).fillna(0)
    hist["absorption_pct_inventory"] = (
        hist["absorption_units_12mo"] / hist["inventory_units"]
    ).fillna(0)
    hist["uc_pct_inventory"] = (
        hist["under_construction"] / hist["inventory_units"]
    ).fillna(0)
    hist["rent_growth_yoy"] = hist["effective_rent_growth_yoy"]

    for col in ENDOG_VARS + ["effective_rent_unit", "inventory_units"]:
        if col in hist.columns:
            hist[col] = hist[col].ffill()

    for col in ENDOG_VARS:
        hist[f"{col}_lag1"] = hist[col].shift(1)
    hist["uc_pct_inventory_lag1"] = hist["uc_pct_inventory"].shift(1)

    # ── CoStar forecasts ──
    if has_is_forecast:
        mask_fc = (raw["is_forecast"] == True) & (raw["star_rating"] == ctx.star_rating)  # noqa: E712
    else:
        mask_fc = (raw["star_rating"] == ctx.star_rating) & raw["date"].astype(str).str.contains(
            "EST", na=False
        )
    fc = raw[mask_fc].copy()
    fc = fc.sort_values("period").drop_duplicates(subset=["period"], keep="last")
    fc = fc.set_index("period")
    if "effective_rent_growth_yoy" in fc.columns:
        fc["rent_growth_yoy"] = fc["effective_rent_growth_yoy"]

    # ── Data-quality gates ──
    if len(hist) < 40:
        raise ValueError(
            f"Insufficient history: need >=40Q, have {len(hist)}Q "
            f"({ctx.parquet_path.name}, tier={ctx.star_rating})"
        )
    expected_periods = pd.period_range(start=hist.index.min(), end=hist.index.max(), freq="Q-DEC")
    missing = len(expected_periods) - len(hist.index)
    if missing > 2:
        raise ValueError(
            f"Too many missing quarters: {missing} gaps in panel "
            f"({hist.index.min()} to {hist.index.max()})"
        )
    if not hist["vacancy_rate"].dropna().between(0, 0.5).all():
        raise ValueError(
            f"Vacancy out of plausible range [0, 0.5] — saw "
            f"min={hist['vacancy_rate'].min()}, max={hist['vacancy_rate'].max()}"
        )
    if not hist["rent_growth_yoy"].dropna().between(-0.30, 0.30).all():
        raise ValueError(
            f"Rent YoY out of plausible range [-30%, +30%] — saw "
            f"min={hist['rent_growth_yoy'].min()}, max={hist['rent_growth_yoy'].max()}"
        )

    return hist, fc


# ═══════════════════════════════════════════════════════════════════════
# 2. DIAGNOSTICS
# ═══════════════════════════════════════════════════════════════════════


def run_diagnostics(panel: pd.DataFrame, label: str = "") -> dict:
    results = {}
    print(f"\n{'=' * 72}")
    print(f"DIAGNOSTIC TESTS — {label}")
    print("=" * 72)

    series = panel["rent_growth_yoy"].dropna()
    print(f"\nSample: {series.index[0]} to {series.index[-1]} ({len(series)} quarters)")

    print("\n─── Descriptive Statistics ───")
    desc = series.describe()
    print(f"  Mean:     {desc['mean']:.4f} ({desc['mean']*100:.2f}%)")
    print(f"  Std Dev:  {desc['std']:.4f} ({desc['std']*100:.2f}%)")
    print(f"  Min:      {desc['min']:.4f} ({desc['min']*100:.2f}%)")
    print(f"  Max:      {desc['max']:.4f} ({desc['max']*100:.2f}%)")
    print(f"  Skewness: {series.skew():.4f}")
    print(f"  Kurtosis: {series.kurtosis():.4f}")
    results["descriptive"] = desc.to_dict()

    print("\n─── Stationarity Tests ───")
    adf_stat, adf_p, adf_lags, adf_nobs, adf_crit, *_ = adfuller(series, maxlag=8)
    print(f"  ADF: stat={adf_stat:.4f}, p={adf_p:.4f} → {'STATIONARY' if adf_p < 0.05 else 'NON-STATIONARY'}")
    results["adf"] = {"stat": adf_stat, "p": adf_p, "stationary": adf_p < 0.05}

    try:
        kpss_stat, kpss_p, *_ = kpss(series, regression="c", nlags="auto")
        print(f"  KPSS: stat={kpss_stat:.4f}, p={kpss_p:.4f} → {'STATIONARY' if kpss_p > 0.05 else 'NON-STATIONARY'}")
        results["kpss"] = {"stat": kpss_stat, "p": kpss_p}
    except Exception:
        pass

    print("\n─── Autocorrelation (Ljung-Box) ───")
    lb = acorr_ljungbox(series, lags=[4, 8, 12], return_df=True)
    for idx, row in lb.iterrows():
        print(f"  Lag {idx}: Q={row['lb_stat']:.1f}, p={row['lb_pvalue']:.4f}")

    print("\n─── Normality (Jarque-Bera) ───")
    jb_stat, jb_p = sp_stats.jarque_bera(series)
    print(f"  JB={jb_stat:.3f}, p={jb_p:.4f} → {'Normal' if jb_p > 0.05 else 'Non-normal'}")

    print("\n─── Structural Break Screening (Rolling σ²) ───")
    rv = series.rolling(window=8).var()
    print(f"  Peak σ² window: {rv.idxmax()} ({rv.max():.6f})")
    print(f"  Min σ² window:  {rv.idxmin()} ({rv.min():.6f})")
    print(f"  Ratio: {rv.max() / max(rv.min(), 1e-10):.1f}x")

    print("\n─── Endogenous Correlations ───")
    avail = [c for c in ENDOG_VARS if c in panel.columns]
    print(panel[avail].dropna().corr().round(3).to_string())

    print("\n─── Granger Causality (vacancy → rent_growth) ───")
    gc_data = panel[["rent_growth_yoy", "vacancy_rate"]].dropna()
    if len(gc_data) > 20:
        try:
            gc = grangercausalitytests(gc_data, maxlag=4, verbose=False)
            for lag, res in gc.items():
                f_p = res[0]["ssr_ftest"][1]
                print(f"  Lag {lag}: p={f_p:.4f} → {'CAUSES' if f_p < 0.05 else 'no'}")
        except Exception as e:
            print(f"  Skipped: {e}")

    print("\n─── Outlier Detection (IQR) ───")
    q1, q3 = series.quantile(0.25), series.quantile(0.75)
    iqr = q3 - q1
    outliers = series[(series < q1 - 1.5 * iqr) | (series > q3 + 1.5 * iqr)]
    print(f"  Outliers: {len(outliers)}")
    for period, val in outliers.items():
        print(f"    {period}: {val*100:.2f}%")

    return results


# ═══════════════════════════════════════════════════════════════════════
# 3. MODEL IMPLEMENTATIONS
# ═══════════════════════════════════════════════════════════════════════


class NaiveModel:
    def __init__(self) -> None:
        self._last_val = None
        self._resid: pd.Series | None = None

    def fit(self, panel: pd.DataFrame) -> None:
        series = panel["rent_growth_yoy"].dropna()
        self._last_val = series.iloc[-1]
        self._resid = series.diff().dropna()

    def predict(self, horizon: int = 8) -> np.ndarray:
        return np.full(horizon, self._last_val)

    def residuals(self) -> pd.Series:
        return self._resid

    @property
    def rmse(self) -> float:
        return float(np.sqrt(np.mean(self._resid**2)))

    def name(self) -> str:
        return "Naive"


class ARIMAModel:
    def __init__(self, max_p: int = 4, max_d: int = 2, max_q: int = 4) -> None:
        self.max_p = max_p
        self.max_d = max_d
        self.max_q = max_q
        self._result = None
        self._order = None
        self._seasonal_order = None
        self._series: pd.Series | None = None
        self._resid: pd.Series | None = None

    def _select_d(self, series: pd.Series) -> int:
        s = series.copy()
        for d in range(self.max_d + 1):
            clean = s.dropna()
            if len(clean) < 10:
                return d
            _, p, *_ = adfuller(clean, maxlag=8)
            if p < 0.05:
                return d
            s = s.diff(1)
        return self.max_d

    def fit(self, panel: pd.DataFrame) -> None:
        self._series = panel["rent_growth_yoy"].dropna()
        if len(self._series) < 16:
            raise ValueError(f"ARIMA needs ≥16 obs, got {len(self._series)}")
        d = self._select_d(self._series)
        best_aic = float("inf")
        best_order = (1, d, 1)
        best_seasonal = None
        for p in range(self.max_p + 1):
            for q in range(self.max_q + 1):
                if p == 0 and q == 0:
                    continue
                for sord in [None, (1, 0, 0, 4), (1, 0, 1, 4), (0, 0, 1, 4)]:
                    try:
                        res = ARIMA(self._series, order=(p, d, q), seasonal_order=sord).fit(
                            method_kwargs={"maxiter": 200}
                        )
                        if res.aic < best_aic:
                            best_aic = res.aic
                            best_order = (p, d, q)
                            best_seasonal = sord
                    except Exception:
                        continue
        self._order = best_order
        self._seasonal_order = best_seasonal
        self._result = ARIMA(self._series, order=best_order, seasonal_order=best_seasonal).fit(
            method_kwargs={"maxiter": 500}
        )
        self._resid = self._result.resid

    def predict(self, horizon: int = 8) -> np.ndarray:
        return self._result.get_forecast(steps=horizon).predicted_mean.values

    def residuals(self) -> pd.Series:
        return self._resid

    @property
    def rmse(self) -> float:
        return float(np.sqrt(np.mean(self._resid**2)))

    def name(self) -> str:
        return f"ARIMA{self._order}"


class BVARModel:
    def __init__(
        self,
        endog_cols: list[str] | None = None,
        max_lags: int = 8,
        minnesota_lambda: float = 0.2,
        cross_shrink: float = 0.5,
    ) -> None:
        self.endog_cols = endog_cols or ENDOG_VARS
        self.max_lags = max_lags
        self.lam = minnesota_lambda
        self.cross = cross_shrink
        self._coefs: dict[str, np.ndarray] = {}
        self._endog: pd.DataFrame | None = None
        self._resid_df: pd.DataFrame | None = None
        self._p: int | None = None

    def fit(self, panel: pd.DataFrame) -> None:
        available = [c for c in self.endog_cols if c in panel.columns]
        self._endog = panel[available].dropna()
        if len(self._endog) < 20:
            raise ValueError(f"BVAR needs ≥20 obs, got {len(self._endog)}")
        model = VAR(self._endog)
        max_lag = min(self.max_lags, len(self._endog) // 5)
        try:
            self._p = max(model.select_order(maxlags=max(max_lag, 1)).selected_orders.get("aic", 2), 1)
        except Exception:
            self._p = 2
        p, k = self._p, len(available)
        Y = self._endog.values
        T = len(Y)
        X = np.column_stack([Y[p - lag : T - lag] for lag in range(1, p + 1)])
        X = np.column_stack([X, np.ones(T - p)])
        Y_dep = Y[p:]
        sigma_sq = np.zeros(k)
        for i in range(k):
            y_i = Y[:, i]
            var_lag = float(np.var(y_i[:-1], ddof=0)) if len(y_i) > 2 else 0
            if var_lag > 0:
                b = float(np.cov(y_i[1:], y_i[:-1], ddof=0)[0, 1] / var_lag)
                sigma_sq[i] = float(np.var(y_i[1:] - b * y_i[:-1], ddof=0))
            else:
                sigma_sq[i] = float(np.var(y_i, ddof=0))
        sigma_sq[sigma_sq == 0] = 1e-10
        n_params = X.shape[1]
        resid_all = np.zeros_like(Y_dep)
        for eq_idx, eq_name in enumerate(self._endog.columns):
            penalty = np.zeros(n_params)
            for lag in range(1, p + 1):
                for vi in range(k):
                    pi = (lag - 1) * k + vi
                    if vi == eq_idx:
                        penalty[pi] = (lag / self.lam) ** 2
                    else:
                        penalty[pi] = (lag / (self.lam * self.cross)) ** 2 * (sigma_sq[eq_idx] / sigma_sq[vi])
            y_eq = Y_dep[:, eq_idx]
            try:
                coef = np.linalg.solve(X.T @ X + np.diag(penalty), X.T @ y_eq)
            except np.linalg.LinAlgError:
                coef = np.linalg.lstsq(X.T @ X + np.diag(penalty), X.T @ y_eq, rcond=None)[0]
            self._coefs[eq_name] = coef
            resid_all[:, eq_idx] = y_eq - X @ coef
        self._resid_df = pd.DataFrame(resid_all, columns=self._endog.columns, index=self._endog.index[p:])

    def predict(self, horizon: int = 8) -> np.ndarray:
        p, k = self._p, len(self._endog.columns)
        y_hist = self._endog.values[-p:].copy()
        forecasts = np.zeros((horizon, k))
        for h in range(horizon):
            x_row = np.concatenate([y_hist[-(lag)] for lag in range(1, p + 1)])
            x_row = np.append(x_row, 1.0)
            y_new = np.array([x_row @ self._coefs[c] for c in self._endog.columns])
            forecasts[h] = y_new
            y_hist = np.vstack([y_hist, y_new])
        return forecasts[:, list(self._endog.columns).index("rent_growth_yoy")]

    def residuals(self) -> pd.Series:
        return self._resid_df["rent_growth_yoy"]

    @property
    def rmse(self) -> float:
        return float(np.sqrt(np.mean(self._resid_df["rent_growth_yoy"] ** 2)))

    def name(self) -> str:
        return f"BVAR(p={self._p})"


FEATURE_CANDIDATES = [
    "vacancy_rate_lag1",
    "deliveries_pct_inventory_lag1",
    "absorption_pct_inventory_lag1",
    "rent_growth_yoy_lag1",
    "uc_pct_inventory_lag1",
]


class RidgeModel:
    def __init__(self, alpha: float = 6.0) -> None:
        self.alpha = alpha
        self._model = None
        self._features: list[str] = []
        self._panel: pd.DataFrame | None = None
        self._resid: pd.Series | None = None
        self._rmse: float | None = None

    def fit(self, panel: pd.DataFrame) -> None:
        self._panel = panel
        self._features = [c for c in FEATURE_CANDIDATES if c in panel.columns]
        train = panel[self._features + ["rent_growth_yoy"]].dropna()
        if len(train) < 20:
            raise ValueError(f"Ridge needs ≥20 obs, got {len(train)}")
        X, y = train[self._features].values, train["rent_growth_yoy"].values
        self._model = Ridge(alpha=self.alpha).fit(X, y)
        yp = self._model.predict(X)
        self._resid = pd.Series(y - yp, index=train.index)
        self._rmse = float(np.sqrt(np.mean((y - yp) ** 2)))

    def predict(self, horizon: int = 8) -> np.ndarray:
        return np.full(horizon, self._model.predict(self._panel[self._features].dropna().iloc[-1:].values)[0])

    def residuals(self) -> pd.Series:
        return self._resid

    @property
    def rmse(self) -> float:
        return self._rmse

    @property
    def coefficients(self) -> dict[str, float]:
        return dict(zip(self._features, self._model.coef_)) if self._model else {}

    def name(self) -> str:
        return "Ridge"


class GBMModel:
    """v2: Tuned to reduce overfitting — fewer trees, shallower depth, subsampling."""

    def __init__(
        self,
        n_estimators: int = 50,
        max_depth: int = 2,
        lr: float = 0.05,
        subsample: float = 0.8,
        colsample: float = 0.8,
        min_child: int = 10,
    ) -> None:
        self.n_estimators = n_estimators
        self.max_depth = max_depth
        self.lr = lr
        self.subsample = subsample
        self.colsample = colsample
        self.min_child = min_child
        self._model = None
        self._features: list[str] = []
        self._panel: pd.DataFrame | None = None
        self._resid: pd.Series | None = None
        self._rmse: float | None = None

    def fit(self, panel: pd.DataFrame) -> None:
        self._panel = panel
        self._features = [c for c in FEATURE_CANDIDATES if c in panel.columns]
        train = panel[self._features + ["rent_growth_yoy"]].dropna()
        if len(train) < 20:
            raise ValueError(f"GBM needs ≥20 obs, got {len(train)}")
        X, y = train[self._features].values, train["rent_growth_yoy"].values
        if HAS_LGB:
            self._model = lgb.LGBMRegressor(
                n_estimators=self.n_estimators,
                max_depth=self.max_depth,
                learning_rate=self.lr,
                subsample=self.subsample,
                colsample_bytree=self.colsample,
                min_child_samples=self.min_child,
                verbose=-1,
            )
        else:
            self._model = GradientBoostingRegressor(
                n_estimators=self.n_estimators,
                max_depth=self.max_depth,
                learning_rate=self.lr,
                subsample=self.subsample,
                min_samples_leaf=self.min_child,
            )
        self._model.fit(X, y)
        yp = self._model.predict(X)
        self._resid = pd.Series(y - yp, index=train.index)
        self._rmse = float(np.sqrt(np.mean((y - yp) ** 2)))

    def predict(self, horizon: int = 8) -> np.ndarray:
        return np.full(horizon, self._model.predict(self._panel[self._features].dropna().iloc[-1:].values)[0])

    def residuals(self) -> pd.Series:
        return self._resid

    @property
    def rmse(self) -> float:
        return self._rmse

    @property
    def feature_importance(self) -> dict[str, float]:
        return dict(zip(self._features, self._model.feature_importances_)) if self._model else {}

    def name(self) -> str:
        return "GBM"


class ElasticNetModel:
    def __init__(self, alpha: float = 1.0, l1_ratio: float = 0.5) -> None:
        self.alpha = alpha
        self.l1_ratio = l1_ratio
        self._model = None
        self._features: list[str] = []
        self._panel: pd.DataFrame | None = None
        self._resid: pd.Series | None = None
        self._rmse: float | None = None

    def fit(self, panel: pd.DataFrame) -> None:
        self._panel = panel
        self._features = [c for c in FEATURE_CANDIDATES if c in panel.columns]
        train = panel[self._features + ["rent_growth_yoy"]].dropna()
        if len(train) < 20:
            raise ValueError(f"ElasticNet needs ≥20 obs, got {len(train)}")
        X, y = train[self._features].values, train["rent_growth_yoy"].values
        self._model = ElasticNet(alpha=self.alpha, l1_ratio=self.l1_ratio, max_iter=5000).fit(X, y)
        yp = self._model.predict(X)
        self._resid = pd.Series(y - yp, index=train.index)
        self._rmse = float(np.sqrt(np.mean((y - yp) ** 2)))

    def predict(self, horizon: int = 8) -> np.ndarray:
        return np.full(horizon, self._model.predict(self._panel[self._features].dropna().iloc[-1:].values)[0])

    def residuals(self) -> pd.Series:
        return self._resid

    @property
    def rmse(self) -> float:
        return self._rmse

    def name(self) -> str:
        return "ElasticNet"


# ═══════════════════════════════════════════════════════════════════════
# 4. ROLLING BACKTEST
# ═══════════════════════════════════════════════════════════════════════

MODEL_SPECS = {
    "Naive": (NaiveModel, {}),
    "ARIMA": (ARIMAModel, {"max_p": 3, "max_d": 2, "max_q": 3}),
    "BVAR": (BVARModel, {"max_lags": 6}),
    "Ridge": (RidgeModel, {"alpha": 6.0}),
    "GBM": (GBMModel, {"n_estimators": 50, "max_depth": 2, "lr": 0.05, "subsample": 0.8, "colsample": 0.8, "min_child": 10}),
    "ElasticNet": (ElasticNetModel, {"alpha": 1.0, "l1_ratio": 0.5}),
}


def rolling_backtest(
    ctx: ForecastContext,
    panel: pd.DataFrame,
    model_class: type,
    model_kwargs: dict | None = None,
) -> pd.DataFrame:
    model_kwargs = model_kwargs or {}
    series = panel["rent_growth_yoy"].dropna()
    results = []
    for t in range(ctx.backtest_min_train, len(series)):
        try:
            m = model_class(**model_kwargs)
            m.fit(panel.iloc[:t].copy())
            pred = m.predict(horizon=1)[0]
            # 80% CI from in-sample residual std (1.28σ ≈ 80%)
            try:
                resid_std = float(m.residuals().std())
            except Exception:
                resid_std = float("nan")
            if resid_std == resid_std:  # not NaN
                pred_lo = pred - 1.28 * resid_std
                pred_hi = pred + 1.28 * resid_std
            else:
                pred_lo = float("nan")
                pred_hi = float("nan")
        except Exception:
            continue
        results.append({
            "period": series.index[t],
            "actual": series.iloc[t],
            "predicted": pred,
            "error": series.iloc[t] - pred,
            "pred_lo": pred_lo,
            "pred_hi": pred_hi,
        })
    return pd.DataFrame(results)


def run_all_backtests(ctx: ForecastContext, panel: pd.DataFrame) -> dict[str, pd.DataFrame]:
    label = f"{ctx.property_name} {ctx.star_rating}"
    print(f"\n{'=' * 72}")
    print(f"ROLLING BACKTEST — {label}")
    print("=" * 72)

    bt_results = {}
    for name, (cls, kwargs) in MODEL_SPECS.items():
        print(f"  {name}...", end=" ", flush=True)
        try:
            bt = rolling_backtest(ctx, panel, cls, kwargs)
            bt_results[name] = bt
            rmse = np.sqrt(np.mean(bt["error"] ** 2))
            mae = np.mean(np.abs(bt["error"]))
            print(f"n={len(bt)}, RMSE={rmse*100:.2f}%, MAE={mae*100:.2f}%")
        except Exception as e:
            print(f"FAILED: {e}")

    # Summary
    print(f"\n{'Model':<14} {'RMSE':>8} {'MAE':>8} {'MedAE':>8} {'MaxErr':>8} {'DM vs Naive':>14}")
    print("─" * 62)
    naive_err = bt_results["Naive"]["error"].values if "Naive" in bt_results else None
    for name, bt in bt_results.items():
        e = bt["error"].values
        rmse = np.sqrt(np.mean(e**2))
        mae = np.mean(np.abs(e))
        medae = np.median(np.abs(e))
        maxerr = np.max(np.abs(e))
        dm = "—"
        if name != "Naive" and naive_err is not None and len(e) == len(naive_err):
            d = naive_err**2 - e**2
            d_var = np.var(d, ddof=1)
            if d_var > 0:
                dm_stat = np.mean(d) / np.sqrt(d_var / len(d))
                dm_p = 2 * (1 - sp_stats.norm.cdf(abs(dm_stat)))
                dm = f"{dm_stat:+.2f} (p={dm_p:.3f})"
        print(f"{name:<14} {rmse:>8.4f} {mae:>8.4f} {medae:>8.4f} {maxerr:>8.4f} {dm:>14}")

    return bt_results


# ═══════════════════════════════════════════════════════════════════════
# 5. FULL-SAMPLE FIT & FORECAST
# ═══════════════════════════════════════════════════════════════════════


def fit_all_models(ctx: ForecastContext, panel: pd.DataFrame) -> dict:
    label = f"{ctx.property_name} {ctx.star_rating}"
    print(f"\n{'=' * 72}")
    print(f"FULL-SAMPLE FIT — {label}")
    print(f"Horizon: {ctx.forecast_horizon}Q ({ctx.forecast_horizon // 4}Y)")
    print("=" * 72)

    results = {}
    for name, (cls, kwargs) in MODEL_SPECS.items():
        try:
            m = cls(**kwargs)
            m.fit(panel)
            preds = m.predict(horizon=ctx.forecast_horizon)
            results[name] = {
                "model": m,
                "predictions": preds,
                "in_sample_rmse": m.rmse,
                "residual_std": float(m.residuals().std()),
            }
            extra = ""
            if hasattr(m, "_order") and m._order:
                extra = f"  order={m._order} seasonal={m._seasonal_order}"
            if hasattr(m, "_p") and m._p:
                extra = f"  lag={m._p}"
            print(f"  {name}: RMSE={m.rmse*100:.2f}%{extra}")
            if hasattr(m, "coefficients") and m.coefficients:
                for f, c in m.coefficients.items():
                    print(f"    {f}: {c:+.6f}")
            if hasattr(m, "feature_importance") and m.feature_importance:
                for f, imp in sorted(m.feature_importance.items(), key=lambda x: -x[1]):
                    print(f"    {f}: {imp:.4f}")
        except Exception as e:
            print(f"  {name}: FAILED — {e}")

    return results


# ═══════════════════════════════════════════════════════════════════════
# 6. ENSEMBLE + SCENARIOS (v2 new)
# ═══════════════════════════════════════════════════════════════════════


def build_ensemble(
    ctx: ForecastContext, model_results: dict, bt_results: dict[str, pd.DataFrame]
) -> dict:
    label = f"{ctx.property_name} {ctx.star_rating}"
    print(f"\n{'=' * 72}")
    print(f"ENSEMBLE — {label}")
    print("=" * 72)

    bt_rmse = {}
    for name, bt in bt_results.items():
        if name == "Naive" or name not in model_results:
            continue
        bt_rmse[name] = np.sqrt(np.mean(bt["error"] ** 2))

    if not bt_rmse:
        return {}

    inv = {m: 1.0 / r for m, r in bt_rmse.items()}
    total = sum(inv.values())
    weights = {m: v / total for m, v in inv.items()}

    print("\n  Weights:")
    for name, w in sorted(weights.items(), key=lambda x: -x[1]):
        print(f"    {name}: {w:.3f} (bt RMSE: {bt_rmse[name]*100:.2f}%)")

    horizon = ctx.forecast_horizon
    point = np.zeros(horizon)
    for name, w in weights.items():
        point += w * model_results[name]["predictions"]

    # Pooled residual std for CI
    all_resid = []
    for name in weights:
        all_resid.extend(model_results[name]["model"].residuals().dropna().values)
    resid_std = float(np.std(all_resid))
    h_scale = np.sqrt(np.arange(1, horizon + 1))

    # Backtest RMSE for scenario bands (more conservative than residual std)
    bt_ensemble_rmse = np.mean(list(bt_rmse.values()))

    ensemble = {
        "weights": weights,
        "point": point,
        "ci_80_lo": point - 1.28 * resid_std * h_scale,
        "ci_80_hi": point + 1.28 * resid_std * h_scale,
        "ci_95_lo": point - 1.96 * resid_std * h_scale,
        "ci_95_hi": point + 1.96 * resid_std * h_scale,
        "resid_std": resid_std,
        # v2: scenario bands calibrated to backtest RMSE
        "bull": point + bt_ensemble_rmse * h_scale,
        "bear": point - bt_ensemble_rmse * h_scale,
        "bt_rmse": bt_ensemble_rmse,
    }

    print(f"\n  Residual std: {resid_std*100:.2f}%")
    print(f"  Backtest RMSE (scenario calibration): {bt_ensemble_rmse*100:.2f}%")
    print(f"\n  {'Q':<6} {'Bear':>8} {'Base':>8} {'Bull':>8}  [80% CI]")
    print("  " + "─" * 50)
    for i in range(horizon):
        print(f"  Q+{i+1:<3} {ensemble['bear'][i]*100:>7.2f}% {point[i]*100:>7.2f}% {ensemble['bull'][i]*100:>7.2f}%"
              f"  [{ensemble['ci_80_lo'][i]*100:.2f}% to {ensemble['ci_80_hi'][i]*100:.2f}%]")

    return ensemble


# ═══════════════════════════════════════════════════════════════════════
# 7. COSTAR COMPARISON + DECOMPOSITION (v2 new)
# ═══════════════════════════════════════════════════════════════════════


def compare_to_costar(
    ctx: ForecastContext,
    model_results: dict,
    ensemble: dict,
    costar_fc: pd.DataFrame,
    panel: pd.DataFrame,
) -> pd.DataFrame:
    label = f"{ctx.property_name} {ctx.star_rating}"
    print(f"\n{'=' * 72}")
    print(f"FORECAST vs COSTAR — {label}")
    print("=" * 72)

    last_p = panel.index[-1]
    fps = [last_p + i for i in range(1, ctx.forecast_horizon + 1)]

    costar_arr = np.array([
        costar_fc.loc[fp, "rent_growth_yoy"] if fp in costar_fc.index else np.nan
        for fp in fps
    ])

    # Quarterly table
    model_names = list(model_results.keys())
    header = f"{'Period':<10} {'CoStar':>8}"
    for n in model_names:
        header += f" {n:>8}"
    header += f" {'Ensemble':>8}"
    print(f"\n{header}")
    print("─" * len(header))

    rows = []
    for i, fp in enumerate(fps):
        row = {"Period": str(fp), "CoStar": costar_arr[i]}
        for n in model_names:
            row[n] = model_results[n]["predictions"][i]
        row["Ensemble"] = ensemble["point"][i]
        row["Bull"] = ensemble["bull"][i]
        row["Bear"] = ensemble["bear"][i]
        row["CI_80_Lo"] = ensemble["ci_80_lo"][i]
        row["CI_80_Hi"] = ensemble["ci_80_hi"][i]
        rows.append(row)

        cs = f"{costar_arr[i]*100:>7.2f}%" if not np.isnan(costar_arr[i]) else f"{'—':>8}"
        line = f"{str(fp):<10} {cs}"
        for n in model_names:
            line += f" {row[n]*100:>7.2f}%"
        line += f" {row['Ensemble']*100:>7.2f}%"
        print(line)

    comp_df = pd.DataFrame(rows)

    # ── Annual averages ──
    print("\n─── Annual Averages ───")
    n_years = ctx.forecast_horizon // 4
    header2 = f"{'Year':<8} {'CoStar':>8}"
    for n in model_names:
        header2 += f" {n:>8}"
    header2 += f" {'Ensem':>8} {'Bear':>8} {'Bull':>8}"
    print(header2)
    print("─" * len(header2))

    for yr in range(n_years):
        s, e = yr * 4, yr * 4 + 4
        if e > len(comp_df):
            break
        year_label = str(fps[s])[:4]
        cs_avg = np.nanmean(costar_arr[s:e])
        line = f"{year_label:<8} {cs_avg*100:>7.2f}%"
        for n in model_names:
            avg = np.mean([comp_df.iloc[j][n] for j in range(s, e)])
            line += f" {avg*100:>7.2f}%"
        ens_avg = np.mean(ensemble["point"][s:e])
        bear_avg = np.mean(ensemble["bear"][s:e])
        bull_avg = np.mean(ensemble["bull"][s:e])
        line += f" {ens_avg*100:>7.2f}% {bear_avg*100:>7.2f}% {bull_avg*100:>7.2f}%"
        print(line)

    # ── v2: Ensemble decomposition ──
    print("\n─── Ensemble Decomposition vs CoStar ───")
    print("  (How much each model pulls ensemble above/below CoStar)\n")
    valid = ~np.isnan(costar_arr)
    if valid.any() and ensemble:
        cs_valid = costar_arr[valid]
        print(f"  {'Model':<14} {'Weight':>7} {'Avg Pred':>9} {'vs CoStar':>10} {'Contribution':>13}")
        print("  " + "─" * 55)
        for name, w in sorted(ensemble["weights"].items(), key=lambda x: -x[1]):
            preds = model_results[name]["predictions"][valid]
            avg_pred = np.mean(preds)
            vs_cs = avg_pred - np.mean(cs_valid)
            contrib = w * vs_cs
            direction = "BULL" if vs_cs > 0 else "BEAR"
            print(f"  {name:<14} {w:>6.3f} {avg_pred*100:>8.2f}% {vs_cs*100:>+9.2f}% {contrib*100:>+12.3f}% [{direction}]")

        total_spread = np.mean(ensemble["point"][valid]) - np.mean(cs_valid)
        print(f"\n  Total ensemble spread vs CoStar: {total_spread*100:+.2f}%")
        print(f"  Direction: {'MORE BULLISH' if total_spread > 0 else 'MORE BEARISH'} than CoStar")

    return comp_df


# ═══════════════════════════════════════════════════════════════════════
# 8. MAIN
# ═══════════════════════════════════════════════════════════════════════


def run_pipeline(ctx: ForecastContext) -> dict:
    """Run full pipeline for the given context."""
    panel, costar_fc = load_panel(ctx)
    print(f"\n  Panel: {len(panel)}Q ({panel.index[0]} to {panel.index[-1]})")
    if len(costar_fc):
        print(f"  CoStar forecasts: {len(costar_fc)}Q ({costar_fc.index[0]} to {costar_fc.index[-1]})")
    else:
        print("  CoStar forecasts: (none in panel)")

    label = f"{ctx.property_name} {ctx.star_rating}"
    diag = run_diagnostics(panel, label)
    bt = run_all_backtests(ctx, panel)
    models = fit_all_models(ctx, panel)
    ens = build_ensemble(ctx, models, bt)
    comp = compare_to_costar(ctx, models, ens, costar_fc, panel)

    ctx.output_dir.mkdir(parents=True, exist_ok=True)
    tag = ctx.tier_slug
    comp.to_csv(ctx.output_dir / f"v2_forecast_{tag}.csv", index=False)
    for name, btdf in bt.items():
        btdf.to_csv(ctx.output_dir / f"v2_backtest_{name.lower()}_{tag}.csv", index=False)
    if ens:
        last_p = panel.index[-1]
        ens_df = pd.DataFrame({
            "period": [str(last_p + i) for i in range(1, ctx.forecast_horizon + 1)],
            "base": ens["point"],
            "bull": ens["bull"],
            "bear": ens["bear"],
            "ci_80_lo": ens["ci_80_lo"],
            "ci_80_hi": ens["ci_80_hi"],
            "ci_95_lo": ens["ci_95_lo"],
            "ci_95_hi": ens["ci_95_hi"],
        })
        ens_df.to_csv(ctx.output_dir / f"v2_ensemble_{tag}.csv", index=False)

    if ens:
        serialize_diagnostics(
            bt_dfs=bt,
            ensemble_meta={
                "weights": ens.get("weights", {}),
                "ensemble_rmse": ens.get("bt_rmse", 0.0),
                "yoy_std": float(panel["rent_growth_yoy"].std()),
                "regime_change": detect_regime_change(panel),
            },
            ctx_meta={
                "metro_slug": ctx.metro_slug,
                "property": ctx.property_name,
                "tier": ctx.star_rating,
                "tier_slug": ctx.tier_slug,
                "panel_quarters": int(len(panel)),
                "panel_start": str(panel.index.min()),
                "panel_end": str(panel.index.max()),
                "forecast_horizon": ctx.forecast_horizon,
            },
            out_path=ctx.output_dir / f"model_diagnostics_{ctx.tier_slug}.json",
        )

    return {
        "panel": panel,
        "costar_fc": costar_fc,
        "bt": bt,
        "models": models,
        "ensemble": ens,
        "comp": comp,
        "diagnostics": diag,
    }


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        description="Submarket rent-growth forecast pipeline (6-model ensemble).",
    )
    parser.add_argument("--config", required=True, help="Property config YAML path")
    parser.add_argument(
        "--star-rating",
        default=None,
        help='Star rating tier (e.g. "1 & 2 Star", "4 & 5 Star", "All"). '
             "Defaults to costar_submarket.primary_star_rating from the config.",
    )
    parser.add_argument(
        "--also-run-all",
        action="store_true",
        help="Also run the All-tier pass for cross-tier comparison.",
    )
    parser.add_argument(
        "--horizon",
        type=int,
        default=8,
        help="Forecast horizon in quarters (default 8 = 2Y)",
    )
    parser.add_argument(
        "--output-dir-override",
        default=None,
        help="Override the output directory derived from config (testing).",
    )
    args = parser.parse_args()

    print("╔══════════════════════════════════════════════════════════════════════╗")
    print("║   SUBMARKET RENT-GROWTH FORECAST SUITE (v2)                         ║")
    print(f"║   Config: {Path(args.config).name:<58s}║")
    horizon_str = f"{args.horizon}Q ({args.horizon // 4}Y)"
    print(f"║   Horizon: {horizon_str:<58s}║")
    print("╚══════════════════════════════════════════════════════════════════════╝")

    primary_ctx = context_from_config(Path(args.config), args.star_rating)
    primary_ctx = _maybe_override_output_dir(primary_ctx, args.output_dir_override)
    primary_ctx = _override_horizon(primary_ctx, args.horizon)
    r_primary = run_pipeline(primary_ctx)

    if args.also_run_all and primary_ctx.star_rating != "All":
        all_ctx = context_from_config(Path(args.config), "All")
        all_ctx = _maybe_override_output_dir(all_ctx, args.output_dir_override)
        all_ctx = _override_horizon(all_ctx, args.horizon)
        r_all = run_pipeline(all_ctx)
        _print_cross_panel(r_primary, r_all, primary_ctx, all_ctx)
        write_cross_tier_csv(primary_ctx, all_ctx, r_primary, r_all)  # ← NEW


def _maybe_override_output_dir(
    ctx: ForecastContext, override: str | None
) -> ForecastContext:
    if not override:
        return ctx
    from dataclasses import replace
    return replace(ctx, output_dir=Path(override))


def _override_horizon(ctx: ForecastContext, horizon: int) -> ForecastContext:
    from dataclasses import replace
    return replace(ctx, forecast_horizon=horizon)


def write_cross_tier_csv(
    ctx_primary: ForecastContext,
    ctx_all: ForecastContext,
    r_primary: dict,
    r_all: dict,
) -> Path:
    """Write a side-by-side comparison of primary tier vs All tier ensembles."""
    if not (r_primary["ensemble"] and r_all["ensemble"]):
        raise ValueError("Both runs must produce ensembles")
    e_p = r_primary["ensemble"]["point"]
    e_a = r_all["ensemble"]["point"]
    last_p = r_primary["panel"].index[-1]
    rows = []
    for i in range(min(len(e_p), len(e_a))):
        period = str(last_p + i + 1)
        rows.append({
            "period": period,
            "primary_yoy": float(e_p[i]),
            "all_yoy": float(e_a[i]),
            "spread": float(e_p[i] - e_a[i]),
        })
    df = pd.DataFrame(rows)
    out = ctx_primary.output_dir / "cross_tier_comparison.csv"
    df.to_csv(out, index=False)
    return out


def _print_cross_panel(
    r_primary: dict, r_all: dict, ctx_p: ForecastContext, ctx_a: ForecastContext
) -> None:
    print(f"\n{'=' * 72}")
    print(f"CROSS-PANEL COMPARISON ({ctx_p.star_rating} vs {ctx_a.star_rating})")
    print("=" * 72)
    if r_primary["ensemble"] and r_all["ensemble"]:
        e_p = r_primary["ensemble"]["point"]
        e_a = r_all["ensemble"]["point"]
        last_p = r_primary["panel"].index[-1]
        print(f"\n  {'Period':<10} {ctx_p.star_rating:>14} {ctx_a.star_rating:>14} {'Spread':>10}")
        print("  " + "─" * 50)
        for i in range(min(len(e_p), len(e_a))):
            fp = str(last_p + i + 1)
            spread = e_p[i] - e_a[i]
            print(f"  {fp:<10} {e_p[i]*100:>13.2f}% {e_a[i]*100:>13.2f}% {spread*100:>+9.2f}%")


if __name__ == "__main__":
    main()
