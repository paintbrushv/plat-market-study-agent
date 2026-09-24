"""Supply-side statistical model and risk scoring for multifamily analysis.

Provides CoStar-based supply risk scoring and optional ridge regression
for rent growth forecasting. Works directly with the snake_case normalized
CoStar parquet schema (not the raw CSV Title Case columns).

The risk scoring framework is transparent and threshold-based — no black-box
ML. Each dimension scores 0–25 points, totaling 0–100 with a clear
PROCEED / CAUTION / PASS verdict.

Usage:
    from etl.supply_model import (
        parse_costar_dates,
        build_submarket_panel,
        score_supply_risk,
        compute_price_elasticity,
    )

    df = pd.read_parquet("data/paid/costar-submarket/xxx.parquet")
    panel = build_submarket_panel(df, star_rating="1 & 2 Star")
    risk = score_supply_risk(panel)
    elasticity = compute_price_elasticity(panel)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import numpy as np
import pandas as pd
import statsmodels.api as sm
from sklearn.linear_model import RidgeCV


# ---------------------------------------------------------------------------
# CoStar date parsing (adapted from research_sandbox/_utils.py for parquets)
# ---------------------------------------------------------------------------

def parse_costar_date(date_str: str) -> tuple[pd.Period, bool]:
    """Parse a CoStar date string into (Period, is_forecast).

    Handles: "2025 Q3", "2026 Q1 EST", "2026 Q1 QTD".
    """
    raw = str(date_str).strip()
    is_forecast = "EST" in raw.upper()
    match = re.match(r"^\s*(\d{4})\s+Q([1-4])(?:\s+.*)?\s*$", raw)
    if not match:
        raise ValueError(f"Unrecognized CoStar date format: {date_str!r}")
    year = int(match.group(1))
    quarter = int(match.group(2))
    return pd.Period(f"{year}Q{quarter}", freq="Q"), is_forecast


def dedupe_costar_dates(df: pd.DataFrame) -> pd.DataFrame:
    """Deduplicate CoStar rows that have both QTD and EST for the same quarter.

    Prefers non-forecast (QTD/actual) rows over EST rows.
    Returns DataFrame with a 'period_q' PeriodIndex column added.
    """
    if "date" not in df.columns:
        return df

    parsed = df["date"].map(parse_costar_date)
    df = df.copy()
    df["period_q"] = [p[0] for p in parsed]
    df["_is_est"] = [p[1] for p in parsed]

    # Prefer non-forecast rows (False sorts before True)
    df = df.sort_values(by=["period_q", "_is_est"], ascending=[True, True])
    df = df.drop_duplicates(subset=["period_q"], keep="first")
    df = df.drop(columns=["_is_est"])
    return df


def infer_forecast_flag(df: pd.DataFrame) -> pd.Series:
    """Infer which rows are forecasts using QTD cutoff heuristic.

    If any QTD rows exist, anything after the latest QTD quarter is forecast.
    Otherwise falls back to the is_forecast column in the parquet.
    """
    if "is_forecast" in df.columns:
        # The parquet already has this flag — use it, but refine with QTD heuristic
        date_str = df["date"].astype(str)
        is_qtd = date_str.str.contains(r"\bQTD\b", case=False, regex=True)

        if is_qtd.any() and "period_q" in df.columns:
            cutoff = df.loc[is_qtd, "period_q"].max()
            return (df["period_q"] > cutoff) | df["is_forecast"]

        return df["is_forecast"].astype(bool)

    return pd.Series(False, index=df.index)


# ---------------------------------------------------------------------------
# Panel construction from parquet schema
# ---------------------------------------------------------------------------

def build_submarket_panel(
    df: pd.DataFrame,
    star_rating: str | None = None,
) -> pd.DataFrame:
    """Build an analysis-ready panel from a CoStar submarket parquet.

    Filters to a star rating, deduplicates dates, adds derived features.
    Returns a DataFrame indexed by pd.PeriodIndex with supply/demand metrics.

    Args:
        df: Raw DataFrame from pd.read_parquet() of a CoStar submarket file.
        star_rating: Filter to this tier (e.g., "1 & 2 Star"). None = all rows.
    """
    if star_rating:
        df = df[df["star_rating"] == star_rating].copy()

    df = dedupe_costar_dates(df)
    df["is_forecast"] = infer_forecast_flag(df)
    df = df.set_index("period_q").sort_index()

    panel = pd.DataFrame(index=df.index)
    panel["is_forecast"] = df["is_forecast"].astype(bool)
    panel["star_rating"] = df.get("star_rating", "All")
    panel["submarket"] = df.get("submarket", "Unknown")

    # Core metrics (all from parquet, already in decimal/unit form)
    panel["vacancy_rate"] = pd.to_numeric(df.get("vacancy_rate"), errors="coerce")
    panel["stabilized_vacancy"] = pd.to_numeric(df.get("stabilized_vacancy"), errors="coerce")
    panel["occupancy_rate"] = pd.to_numeric(df.get("occupancy_rate"), errors="coerce")
    panel["vacancy_gap"] = panel["vacancy_rate"] - panel["stabilized_vacancy"]

    panel["inventory_units"] = pd.to_numeric(df.get("inventory_units"), errors="coerce")
    panel["under_construction"] = pd.to_numeric(df.get("under_construction"), errors="coerce").fillna(0)
    panel["deliveries"] = pd.to_numeric(df.get("deliveries"), errors="coerce").fillna(0)
    panel["deliveries_12mo"] = pd.to_numeric(df.get("deliveries_12mo"), errors="coerce").fillna(0)
    panel["deliveries_gross"] = pd.to_numeric(df.get("deliveries_gross"), errors="coerce").fillna(0)
    panel["absorption_units_12mo"] = pd.to_numeric(df.get("absorption_units_12mo"), errors="coerce")
    panel["construction_starts_12mo"] = pd.to_numeric(df.get("construction_starts_12mo"), errors="coerce").fillna(0)

    # Rents
    panel["effective_rent_unit"] = pd.to_numeric(df.get("effective_rent_unit"), errors="coerce")
    panel["asking_rent_unit"] = pd.to_numeric(df.get("asking_rent_unit"), errors="coerce")
    panel["effective_rent_growth_yoy"] = pd.to_numeric(df.get("effective_rent_growth_yoy"), errors="coerce")
    panel["asking_rent_growth_yoy"] = pd.to_numeric(df.get("asking_rent_growth_yoy"), errors="coerce")

    # Derived share metrics (denominator = inventory)
    inv = panel["inventory_units"].replace({0: np.nan})
    panel["uc_share"] = panel["under_construction"] / inv
    panel["deliveries_12mo_share"] = panel["deliveries_12mo"] / inv
    panel["absorption_12mo_share"] = panel["absorption_units_12mo"] / inv

    # Absorption-to-delivery ratio (key supply health indicator)
    del_12 = panel["deliveries_12mo"].replace({0: np.nan})
    panel["absorption_delivery_ratio"] = panel["absorption_units_12mo"] / del_12

    # --- Cumulative surplus / equilibrium metrics ---
    # These track the ACCUMULATED overhang, not just the rolling 12-month snapshot.
    # A 2,000-unit delivery wave in 2024 with only 1,000 absorbed creates a 1,000-unit
    # surplus that persists into 2025 even as rolling metrics "reset."

    # Implied quarterly absorption = change in occupied units
    occupied = panel["inventory_units"] * (1 - panel["vacancy_rate"])
    panel["occupied_units"] = occupied
    panel["quarterly_absorption_implied"] = occupied.diff()

    # Quarterly excess: deliveries minus implied absorption
    panel["quarterly_excess"] = panel["deliveries"].fillna(0) - panel["quarterly_absorption_implied"].fillna(0)

    # Cumulative net surplus from series start
    # Positive = accumulated units sitting vacant above equilibrium
    panel["cumulative_net_surplus"] = panel["quarterly_excess"].cumsum()

    # Long-run equilibrium vacancy (rolling 20-quarter = 5-year average)
    panel["equilibrium_vacancy"] = panel["vacancy_rate"].expanding(min_periods=8).mean()

    # Units to equilibrium: how many units must absorb to reach equilibrium vacancy
    # = (current_vacancy - equilibrium_vacancy) * inventory
    panel["units_to_equilibrium"] = (
        (panel["vacancy_rate"] - panel["equilibrium_vacancy"]) * panel["inventory_units"]
    )

    # Quarters to equilibrium: at recent absorption pace, how long to clear?
    # Uses trailing 4Q average absorption (positive = absorbing = healing)
    recent_abs_pace = panel["quarterly_absorption_implied"].rolling(4, min_periods=2).mean()
    # Only meaningful when absorption is positive (market is healing)
    panel["quarters_to_equilibrium"] = np.where(
        recent_abs_pace > 0,
        panel["units_to_equilibrium"] / recent_abs_pace,
        np.nan,  # Infinite if market is still losing tenants
    )

    # Delivery-vs-growth ratio: deliveries relative to organic inventory growth
    # Uses trailing inventory growth as a proxy for household formation
    inv_growth_4q = panel["inventory_units"].pct_change(4)  # YoY inventory growth rate
    panel["inventory_growth_yoy"] = inv_growth_4q

    # Excess delivery ratio: how much are deliveries exceeding organic growth?
    # >1.0 = deliveries exceed what the market naturally absorbs
    # <1.0 = deliveries within natural absorption capacity
    organic_absorption = panel["inventory_units"].shift(4) * inv_growth_4q.rolling(8).mean()
    organic_absorption = organic_absorption.clip(lower=0)  # Can't have negative organic absorption
    panel["delivery_vs_organic_growth"] = panel["deliveries_12mo"] / organic_absorption.replace({0: np.nan})

    # Supply pressure index: deliveries minus absorption, normalized by inventory
    # Positive = supply outpacing demand (pressure on rents)
    # Negative = demand outpacing supply (supports rent growth)
    panel["supply_pressure"] = (panel["deliveries_12mo"] - panel["absorption_units_12mo"]) / inv

    return panel


def build_cross_tier_panel(
    df: pd.DataFrame,
    subject_star_rating: str = "1 & 2 Star",
) -> pd.DataFrame:
    """Build a panel with cross-tier supply features for the subject tier.

    Loads ALL star tiers from the same submarket parquet, computes metrics
    for the higher tiers (4&5 Star, 3 Star), and adds them as features to
    the subject tier panel. This captures the filtration effect where Class A
    deliveries pull tenants from workforce housing.

    Args:
        df: Raw DataFrame from pd.read_parquet() — must contain all star tiers.
        subject_star_rating: The tier to analyze (e.g., "1 & 2 Star").

    Returns:
        Panel for the subject tier with cross-tier features added.
    """
    # Build the subject tier panel
    panel = build_submarket_panel(df, star_rating=subject_star_rating)
    if panel.empty:
        return panel

    # Build panels for other tiers
    tier_45 = build_submarket_panel(df, star_rating="4 & 5 Star")
    tier_3 = build_submarket_panel(df, star_rating="3 Star")
    tier_all = build_submarket_panel(df, star_rating="All")

    # Add 4&5 Star cross-tier features (the primary filtration driver)
    if not tier_45.empty:
        cross_45 = pd.DataFrame(index=tier_45.index)
        cross_45["class_a_vacancy_rate"] = tier_45["vacancy_rate"]
        cross_45["class_a_eff_rent_growth_yoy"] = tier_45["effective_rent_growth_yoy"]
        cross_45["class_a_deliveries_12mo"] = tier_45["deliveries_12mo"]
        cross_45["class_a_absorption_12mo"] = tier_45["absorption_units_12mo"]
        cross_45["class_a_uc_units"] = tier_45["under_construction"]
        cross_45["class_a_inventory"] = tier_45["inventory_units"]

        # Derived: Class A deliveries as share of TOTAL submarket inventory
        if not tier_all.empty:
            total_inv = tier_all["inventory_units"]
            cross_45["class_a_deliveries_share_of_total"] = (
                tier_45["deliveries_12mo"] / total_inv.reindex(tier_45.index).replace({0: np.nan})
            )
            cross_45["class_a_uc_share_of_total"] = (
                tier_45["under_construction"] / total_inv.reindex(tier_45.index).replace({0: np.nan})
            )

        # Rent gap: difference between Class A and subject effective rent
        if "effective_rent_unit" in tier_45.columns and "effective_rent_unit" in panel.columns:
            class_a_rent = tier_45["effective_rent_unit"].reindex(panel.index)
            subject_rent = panel["effective_rent_unit"]
            cross_45["rent_gap_class_a_vs_subject"] = class_a_rent - subject_rent
            cross_45["rent_premium_pct"] = (class_a_rent / subject_rent.replace({0: np.nan})) - 1

        panel = panel.join(cross_45, how="left", rsuffix="_45")

    # Add 3 Star features (secondary filtration)
    if not tier_3.empty:
        cross_3 = pd.DataFrame(index=tier_3.index)
        cross_3["mid_tier_vacancy_rate"] = tier_3["vacancy_rate"]
        cross_3["mid_tier_deliveries_12mo"] = tier_3["deliveries_12mo"]
        cross_3["mid_tier_eff_rent_growth_yoy"] = tier_3["effective_rent_growth_yoy"]
        panel = panel.join(cross_3, how="left", rsuffix="_3")

    # Add total submarket metrics
    if not tier_all.empty:
        cross_all = pd.DataFrame(index=tier_all.index)
        cross_all["total_submarket_deliveries_12mo"] = tier_all["deliveries_12mo"]
        cross_all["total_submarket_vacancy_rate"] = tier_all["vacancy_rate"]
        cross_all["total_submarket_absorption_12mo"] = tier_all["absorption_units_12mo"]
        cross_all["total_submarket_inventory"] = tier_all["inventory_units"]
        panel = panel.join(cross_all, how="left", rsuffix="_all")

        # --- Total-submarket equilibrium metrics ---
        # These capture the cumulative surplus across ALL tiers, which is what
        # drives the vacancy overhang that affects workforce housing for years
        # after a delivery wave.
        if "cumulative_net_surplus" in tier_all.columns:
            panel["total_cumulative_surplus"] = tier_all["cumulative_net_surplus"].reindex(panel.index)
        if "units_to_equilibrium" in tier_all.columns:
            panel["total_units_to_equilibrium"] = tier_all["units_to_equilibrium"].reindex(panel.index)
        if "quarters_to_equilibrium" in tier_all.columns:
            panel["total_quarters_to_eq"] = tier_all["quarters_to_equilibrium"].reindex(panel.index)
        if "supply_pressure" in tier_all.columns:
            panel["total_supply_pressure"] = tier_all["supply_pressure"].reindex(panel.index)
        if "equilibrium_vacancy" in tier_all.columns:
            panel["total_eq_vacancy"] = tier_all["equilibrium_vacancy"].reindex(panel.index)

    return panel


# ---------------------------------------------------------------------------
# Cycle phase detection (lightweight, no scipy dependency)
# ---------------------------------------------------------------------------

def detect_cycle_phase(panel: pd.DataFrame, lookback: int = 8) -> pd.Series:
    """Detect market cycle phase from vacancy rate trend.

    Uses a rolling window to classify each quarter into one of four phases:
    - EXPANSION: vacancy falling, below long-term average
    - PEAK: vacancy rising, below long-term average (turning point)
    - CONTRACTION: vacancy rising, above long-term average
    - TROUGH: vacancy falling, above long-term average (recovery starting)

    This is a simplified version of the Kalman filter approach in supply-demand
    repo — purely mechanical, no state-space estimation. Suitable as a
    feature input for the rent growth model.

    Args:
        panel: DataFrame with 'vacancy_rate' column and PeriodIndex.
        lookback: Quarters for rolling trend calculation.

    Returns:
        Series of cycle phase labels aligned to panel index.
    """
    if "vacancy_rate" not in panel.columns:
        return pd.Series("UNKNOWN", index=panel.index)

    vac = panel["vacancy_rate"].copy()
    vac_avg = vac.rolling(20, min_periods=8).mean()  # ~5yr rolling average
    vac_trend = vac.rolling(lookback, min_periods=4).apply(
        lambda x: np.polyfit(range(len(x)), x, 1)[0] if len(x) >= 4 else 0,
        raw=True,
    )

    phases = []
    for i in range(len(vac)):
        v = vac.iloc[i]
        avg = vac_avg.iloc[i]
        trend = vac_trend.iloc[i]

        if pd.isna(v) or pd.isna(avg) or pd.isna(trend):
            phases.append("UNKNOWN")
        elif v < avg and trend <= 0:
            phases.append("EXPANSION")
        elif v < avg and trend > 0:
            phases.append("PEAK")
        elif v >= avg and trend > 0:
            phases.append("CONTRACTION")
        else:
            phases.append("TROUGH")

    return pd.Series(phases, index=panel.index, name="cycle_phase")


def detect_cycle_phase_hilbert(
    panel: pd.DataFrame,
    bandpass_periods: tuple[int, int] = (16, 32),
    column: str = "vacancy_rate",
) -> pd.DataFrame:
    """Detect cycle phase using Hilbert transform with bandpass filtering.

    Uses scipy's signal processing to extract the instantaneous phase of the
    market cycle from the vacancy rate (or other oscillating series). This is
    the proper frequency-domain approach from the supply-demand repo.

    The bandpass filter isolates the 4-8 year cycle (16-32 quarters), removing
    seasonality and long-term trend.

    Args:
        panel: DataFrame with a vacancy_rate or other oscillating column.
        bandpass_periods: (min_quarters, max_quarters) for the cycle band.
            Default (16, 32) targets the 4-8 year RE cycle.
        column: Column to analyze.

    Returns:
        DataFrame with cycle features: hilbert_phase, hilbert_amplitude,
        hilbert_phase_sin, hilbert_phase_cos, cycle_phase_hilbert.
    """
    try:
        from scipy.signal import butter, filtfilt, hilbert
    except ImportError:
        return pd.DataFrame(index=panel.index)

    series = panel[column].copy()
    if series.isna().sum() > len(series) * 0.3:
        return pd.DataFrame(index=panel.index)

    # Interpolate NaN for filtering
    series = series.interpolate(method="linear").bfill().ffill()
    x = series.to_numpy(dtype=np.float64)

    if len(x) < bandpass_periods[1] * 2:
        return pd.DataFrame(index=panel.index)

    # Detrend
    x_detrended = x - np.polyval(np.polyfit(range(len(x)), x, 1), range(len(x)))

    # Bandpass filter (Butterworth)
    low_freq = 1.0 / bandpass_periods[1]  # cycles per quarter
    high_freq = 1.0 / bandpass_periods[0]
    nyquist = 0.5  # 1 sample per quarter

    try:
        b, a = butter(2, [low_freq / nyquist, high_freq / nyquist], btype="band")
        x_filtered = filtfilt(b, a, x_detrended)
    except ValueError:
        return pd.DataFrame(index=panel.index)

    # Hilbert transform
    analytic = hilbert(x_filtered)
    phase = np.angle(analytic)       # instantaneous phase (-π to π)
    amplitude = np.abs(analytic)     # instantaneous amplitude

    result = pd.DataFrame(index=panel.index)
    result["hilbert_phase"] = phase
    result["hilbert_amplitude"] = amplitude
    result["hilbert_phase_sin"] = np.sin(phase)
    result["hilbert_phase_cos"] = np.cos(phase)

    # Map phase to cycle labels (0→π = expansion→peak, π→2π = contraction→trough)
    phase_mod = phase % (2 * np.pi)  # 0 to 2π
    labels = []
    for p in phase_mod:
        if p < np.pi / 2:
            labels.append("EXPANSION")
        elif p < np.pi:
            labels.append("PEAK")
        elif p < 3 * np.pi / 2:
            labels.append("CONTRACTION")
        else:
            labels.append("TROUGH")
    result["cycle_phase_hilbert"] = labels

    return result


def extract_cycle_features(panel: pd.DataFrame) -> pd.DataFrame:
    """Extract all cycle-related features from a panel.

    Combines both simple trend-based and Hilbert transform methods.
    Returns a DataFrame of features ready to join to the analysis panel.
    """
    features = pd.DataFrame(index=panel.index)

    # Simple trend-based
    simple_phase = detect_cycle_phase(panel)
    simple_dummies = encode_cycle_phase(simple_phase)
    features = features.join(simple_dummies)

    # Hilbert transform (requires scipy)
    hilbert_features = detect_cycle_phase_hilbert(panel)
    if not hilbert_features.empty:
        # Use sin/cos of phase (continuous, regression-friendly)
        for col in ["hilbert_phase_sin", "hilbert_phase_cos", "hilbert_amplitude"]:
            if col in hilbert_features.columns:
                features[col] = hilbert_features[col]

        # Also add Hilbert phase dummies
        if "cycle_phase_hilbert" in hilbert_features.columns:
            h_dummies = pd.get_dummies(hilbert_features["cycle_phase_hilbert"], prefix="hcycle")
            if "hcycle_EXPANSION" in h_dummies.columns:
                h_dummies = h_dummies.drop(columns=["hcycle_EXPANSION"])
            features = features.join(h_dummies)

    return features


def encode_cycle_phase(phase_series: pd.Series) -> pd.DataFrame:
    """One-hot encode cycle phases for use as regression features."""
    dummies = pd.get_dummies(phase_series, prefix="cycle")
    # Drop one phase to avoid multicollinearity (EXPANSION is the reference)
    if "cycle_EXPANSION" in dummies.columns:
        dummies = dummies.drop(columns=["cycle_EXPANSION"])
    return dummies


def build_metro_panel(
    parquet_dir: str,
    star_rating: str | None = None,
) -> pd.DataFrame:
    """Build an inventory-weighted metro aggregate from all submarket parquets.

    Loads every .parquet in the directory, filters to star_rating, and
    aggregates using inventory-weighted averages for rates and sums for counts.

    Args:
        parquet_dir: Directory containing CoStar submarket parquet files.
        star_rating: Filter tier. None = "All" tier from each file.
    """
    from pathlib import Path

    parquet_dir_path = Path(parquet_dir)
    files = sorted(parquet_dir_path.glob("*.parquet"))
    if not files:
        return pd.DataFrame()

    frames = []
    for f in files:
        try:
            df = pd.read_parquet(f)
            sub_panel = build_submarket_panel(df, star_rating=star_rating)
            sub_panel["_source_file"] = f.stem
            frames.append(sub_panel)
        except Exception:
            continue

    if not frames:
        return pd.DataFrame()

    combined = pd.concat(frames)

    # Aggregate by period: sums for counts, inventory-weighted avg for rates
    def _weighted_avg(group: pd.DataFrame, col: str, weight_col: str = "inventory_units") -> float:
        valid = group[[col, weight_col]].dropna()
        if valid.empty or valid[weight_col].sum() == 0:
            return np.nan
        return float((valid[col] * valid[weight_col]).sum() / valid[weight_col].sum())

    periods = sorted(combined.index.unique())
    rows = []
    for period in periods:
        g = combined.loc[[period]] if period in combined.index else pd.DataFrame()
        if g.empty:
            continue

        row = {
            "is_forecast": bool(g["is_forecast"].any()),
            "inventory_units": g["inventory_units"].sum(),
            "under_construction": g["under_construction"].sum(),
            "deliveries": g["deliveries"].sum(),
            "deliveries_12mo": g["deliveries_12mo"].sum(),
            "absorption_units_12mo": g["absorption_units_12mo"].sum(),
            "construction_starts_12mo": g["construction_starts_12mo"].sum(),
            "vacancy_rate": _weighted_avg(g, "vacancy_rate"),
            "effective_rent_unit": _weighted_avg(g, "effective_rent_unit"),
            "effective_rent_growth_yoy": _weighted_avg(g, "effective_rent_growth_yoy"),
            "asking_rent_growth_yoy": _weighted_avg(g, "asking_rent_growth_yoy"),
            "submarket_count": len(g),
        }

        inv = row["inventory_units"] or np.nan
        row["uc_share"] = row["under_construction"] / inv if inv else np.nan
        row["deliveries_12mo_share"] = row["deliveries_12mo"] / inv if inv else np.nan

        # Supply pressure: excess deliveries over absorption, normalized
        abs_12 = row["absorption_units_12mo"]
        del_12 = row["deliveries_12mo"]
        row["supply_pressure"] = (del_12 - abs_12) / inv if inv else np.nan
        row["absorption_delivery_ratio"] = abs_12 / del_12 if del_12 > 0 else (2.0 if abs_12 >= 0 else 0.0)

        rows.append((period, row))

    if not rows:
        return pd.DataFrame()

    metro = pd.DataFrame.from_dict(dict(rows), orient="index")
    metro.index = pd.PeriodIndex([r[0] for r in rows], freq="Q")
    metro.index.name = "period_q"

    # Add inventory growth and delivery-vs-growth ratio (needs time series context)
    metro["inventory_growth_yoy"] = metro["inventory_units"].pct_change(4)
    organic = metro["inventory_units"].shift(4) * metro["inventory_growth_yoy"].rolling(8).mean()
    organic = organic.clip(lower=0)
    metro["delivery_vs_organic_growth"] = metro["deliveries_12mo"] / organic.replace({0: np.nan})

    return metro


# ---------------------------------------------------------------------------
# Supply risk scoring
# ---------------------------------------------------------------------------

# Transparent threshold table — no black-box formula
DEFAULT_THRESHOLDS = {
    "pipeline_depth": {
        "metric": "uc_share",
        "high_risk": 0.10,     # UC > 10% of inventory
        "moderate": 0.05,      # UC 5–10%
        "max_points": 25,
    },
    "delivery_timing": {
        "metric": "deliveries_next_4q_share",
        "high_risk": 0.08,     # Next 4Q deliveries > 8% of inventory
        "moderate": 0.04,      # 4–8%
        "max_points": 25,
    },
    "absorption_trend": {
        "metric": "absorption_delivery_ratio_4q",
        "high_risk": 0.7,      # Abs/Del < 0.7 (market can't absorb)
        "moderate": 1.0,       # 0.7–1.0 (tight)
        "max_points": 25,
        "invert": True,        # Higher is better (inverted scoring)
    },
    "rent_momentum": {
        "metric": "effective_rent_growth_yoy_latest",
        "high_risk": -0.03,    # Eff rent growth < -3%
        "moderate": 0.0,       # -3% to 0%
        "max_points": 25,
        "invert": True,        # Higher is better
    },
}


@dataclass
class SupplyRiskDimension:
    name: str
    raw_value: float
    score: int
    max_points: int
    signal: str  # "LOW", "MODERATE", "HIGH"
    note: str


@dataclass
class SupplyRiskScore:
    total_score: int
    max_score: int
    verdict: str  # "PROCEED", "CAUTION", "PASS"
    dimensions: list[SupplyRiskDimension]
    as_of_quarter: str
    submarket: str
    star_rating: str


def _score_dimension(
    value: float,
    high_risk_threshold: float,
    moderate_threshold: float,
    max_points: int,
    invert: bool = False,
) -> tuple[int, str]:
    """Score a single dimension. Returns (points, signal)."""
    if np.isnan(value):
        return max_points // 2, "UNKNOWN"

    if invert:
        # Higher values are better (e.g., absorption ratio, rent growth)
        if value >= moderate_threshold:
            return max_points, "LOW"
        elif value > high_risk_threshold:
            # Linear interpolation: 1 point at high_risk, (max-1) at moderate
            frac = (value - high_risk_threshold) / (moderate_threshold - high_risk_threshold)
            return max(1, min(max_points - 1, int(max_points * frac))), "MODERATE"
        else:
            return 0, "HIGH"
    else:
        # Lower values are better (e.g., pipeline depth, delivery timing)
        if value < moderate_threshold:
            return max_points, "LOW"
        elif value < high_risk_threshold:
            # Linear interpolation: (max-1) near moderate, 1 near high_risk
            frac = 1.0 - (value - moderate_threshold) / (high_risk_threshold - moderate_threshold)
            return max(1, min(max_points - 1, int(max_points * frac))), "MODERATE"
        else:
            return 0, "HIGH"


def score_supply_risk(
    panel: pd.DataFrame,
    thresholds: dict | None = None,
) -> SupplyRiskScore:
    """Score supply risk on a 0–100 scale with transparent component breakdown.

    Uses the most recent actual data point and trailing 4-quarter averages.
    Returns a SupplyRiskScore with per-dimension detail.

    Args:
        panel: DataFrame from build_submarket_panel() with PeriodIndex.
        thresholds: Override DEFAULT_THRESHOLDS if desired.
    """
    thresholds = thresholds or DEFAULT_THRESHOLDS

    # Get actuals only
    actuals = panel[~panel["is_forecast"]].copy()
    if actuals.empty:
        return SupplyRiskScore(
            total_score=0, max_score=100, verdict="UNKNOWN",
            dimensions=[], as_of_quarter="N/A",
            submarket="Unknown", star_rating="Unknown",
        )

    latest = actuals.iloc[-1]
    last_4q = actuals.tail(4)
    as_of = str(actuals.index[-1])

    submarket = str(latest.get("submarket", "Unknown"))
    star_rating = str(latest.get("star_rating", "Unknown"))

    # Compute metric values
    metrics = {}

    # 1. Pipeline depth: UC / inventory (latest)
    metrics["uc_share"] = float(latest.get("uc_share", np.nan))

    # 2. Delivery timing: sum of next 4 forecast quarters / inventory
    forecasts = panel[panel["is_forecast"]].head(4)
    if not forecasts.empty and latest.get("inventory_units", 0) > 0:
        next_4q_deliveries = forecasts["deliveries"].sum()
        metrics["deliveries_next_4q_share"] = next_4q_deliveries / latest["inventory_units"]
    else:
        metrics["deliveries_next_4q_share"] = float(latest.get("deliveries_12mo_share", np.nan))

    # 3. Absorption trend: trailing 4Q sum of quarterly deliveries vs quarterly absorption
    # Use quarterly 'deliveries' and 'absorption_units_12mo' (the latter is already rolling,
    # so for a true 4Q sum we use 'deliveries' quarterly column summed)
    del_4q_sum = last_4q["deliveries"].sum()
    # For absorption, use the latest 12mo figure (it already covers the trailing 4Q period)
    abs_latest = float(latest.get("absorption_units_12mo", np.nan))
    if del_4q_sum > 0:
        metrics["absorption_delivery_ratio_4q"] = abs_latest / del_4q_sum
    elif not np.isnan(abs_latest) and abs_latest >= 0:
        metrics["absorption_delivery_ratio_4q"] = 2.0  # No deliveries + positive absorption = great
    elif not np.isnan(abs_latest):
        metrics["absorption_delivery_ratio_4q"] = 0.0  # Negative absorption + no deliveries = bad
    else:
        metrics["absorption_delivery_ratio_4q"] = np.nan

    # 4. Rent growth momentum: latest YoY effective rent growth
    metrics["effective_rent_growth_yoy_latest"] = float(latest.get("effective_rent_growth_yoy", np.nan))

    # Score each dimension
    dimensions = []
    total_score = 0
    max_score = 0

    dim_configs = [
        ("Pipeline Depth", "pipeline_depth", "uc_share",
         f"UC = {metrics['uc_share']:.1%} of inventory" if not np.isnan(metrics.get('uc_share', np.nan)) else "N/A"),
        ("Delivery Timing", "delivery_timing", "deliveries_next_4q_share",
         f"Next 4Q = {metrics['deliveries_next_4q_share']:.1%} of inventory" if not np.isnan(metrics.get('deliveries_next_4q_share', np.nan)) else "N/A"),
        ("Absorption Trend", "absorption_trend", "absorption_delivery_ratio_4q",
         f"4Q avg abs/del = {metrics['absorption_delivery_ratio_4q']:.2f}" if not np.isnan(metrics.get('absorption_delivery_ratio_4q', np.nan)) else "N/A"),
        ("Rent Momentum", "rent_momentum", "effective_rent_growth_yoy_latest",
         f"Eff rent YoY = {metrics['effective_rent_growth_yoy_latest']:+.1%}" if not np.isnan(metrics.get('effective_rent_growth_yoy_latest', np.nan)) else "N/A"),
    ]

    for name, key, metric_key, note in dim_configs:
        t = thresholds[key]
        value = metrics.get(metric_key, np.nan)
        pts, signal = _score_dimension(
            value,
            t["high_risk"],
            t["moderate"],
            t["max_points"],
            invert=t.get("invert", False),
        )
        dimensions.append(SupplyRiskDimension(
            name=name, raw_value=value, score=pts,
            max_points=t["max_points"], signal=signal, note=note,
        ))
        total_score += pts
        max_score += t["max_points"]

    # Verdict
    if total_score >= 70:
        verdict = "PROCEED"
    elif total_score >= 40:
        verdict = "CAUTION"
    else:
        verdict = "PASS"

    return SupplyRiskScore(
        total_score=total_score,
        max_score=max_score,
        verdict=verdict,
        dimensions=dimensions,
        as_of_quarter=as_of,
        submarket=submarket,
        star_rating=star_rating,
    )


# ---------------------------------------------------------------------------
# Price elasticity estimation (simple OLS)
# ---------------------------------------------------------------------------

@dataclass
class ElasticityResult:
    coefficients: dict[str, float]
    r_squared: float
    n_observations: int
    features: list[str]
    low_confidence: bool = False


def compute_price_elasticity(panel: pd.DataFrame) -> ElasticityResult:
    """Estimate rent growth elasticity via OLS on actual quarters.

    Regression: effective_rent_growth_yoy ~ vacancy_gap + deliveries_12mo_share + absorption_12mo_share

    Uses 1-quarter lagged features to avoid look-ahead bias.
    Requires ≥20 actual observations; sets low_confidence flag if fewer.

    Args:
        panel: DataFrame from build_submarket_panel().

    Returns:
        ElasticityResult with coefficients and R².
    """
    actuals = panel[~panel["is_forecast"]].copy()

    features = ["vacancy_gap", "deliveries_12mo_share", "absorption_12mo_share"]
    target = "effective_rent_growth_yoy"

    # Lag features by 1 quarter
    lagged = pd.DataFrame(index=actuals.index)
    for f in features:
        lagged[f"{f}_lag1"] = actuals[f].shift(1)
    lagged[target] = actuals[target]
    lagged = lagged.dropna()

    feature_cols = [f"{f}_lag1" for f in features]
    n = len(lagged)

    if n < 8:
        return ElasticityResult(
            coefficients={}, r_squared=0.0, n_observations=n,
            features=feature_cols, low_confidence=True,
        )

    # Drop constant columns (e.g., all-zero deliveries) to avoid singular matrix
    non_constant_cols = [c for c in feature_cols if lagged[c].std() > 1e-10]
    if not non_constant_cols:
        return ElasticityResult(
            coefficients={}, r_squared=0.0, n_observations=n,
            features=feature_cols, low_confidence=True,
        )

    X = lagged[non_constant_cols].to_numpy(dtype=np.float64)
    y = lagged[target].to_numpy(dtype=np.float64)

    X_with_intercept = sm.add_constant(X)
    model = sm.OLS(y, X_with_intercept).fit()

    coefficients = {"intercept": float(model.params[0])}
    for i, col in enumerate(non_constant_cols):
        coefficients[col] = float(model.params[i + 1])
    for col in feature_cols:
        if col not in non_constant_cols:
            coefficients[col] = 0.0

    return ElasticityResult(
        coefficients=coefficients,
        r_squared=float(model.rsquared),
        n_observations=n,
        features=non_constant_cols,
        low_confidence=n < 20,
    )


# ---------------------------------------------------------------------------
# Ridge regression (extracted from research_sandbox)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RidgeFitResult:
    feature_names: list[str]
    coef: np.ndarray       # shape (n_features,)
    intercept: float       # y_mean
    x_mean: np.ndarray     # per-feature training mean
    x_std: np.ndarray      # per-feature training std
    train_n: int
    train_rmse: float


def fit_ridge(
    panel: pd.DataFrame,
    target: str,
    feature_names: list[str],
    alphas: list[float] | None = None,
) -> RidgeFitResult | None:
    """Fit ridge regression on non-forecast rows with cross-validated alpha.

    Standardizes features, centers target, uses RidgeCV for alpha selection.
    Returns None if insufficient data.
    """
    actuals = panel[~panel["is_forecast"]].copy()
    x = actuals[feature_names].to_numpy(dtype=np.float64)
    y = actuals[target].to_numpy(dtype=np.float64)

    # Drop rows with any NaN
    mask = np.isfinite(y)
    for i in range(x.shape[1]):
        mask &= np.isfinite(x[:, i])
    x, y = x[mask], y[mask]

    if len(y) < 20:
        return None

    if alphas is None:
        alphas = [0.1, 0.5, 1.0, 2.0, 4.0, 6.0, 10.0, 20.0, 50.0]

    # Standardize
    x_mean = np.nanmean(x, axis=0)
    x_std = np.nanstd(x, axis=0)
    x_std = np.where(x_std == 0, 1.0, x_std)
    xz = (x - x_mean) / x_std

    y_mean = float(np.mean(y))
    y0 = y - y_mean

    # Cross-validated alpha selection
    ridge = RidgeCV(alphas=alphas, fit_intercept=False)
    ridge.fit(xz, y0)
    coef = ridge.coef_

    # Training RMSE
    y_pred = y_mean + xz @ coef
    train_rmse = float(np.sqrt(np.mean((y - y_pred) ** 2)))

    return RidgeFitResult(
        feature_names=feature_names,
        coef=coef,
        intercept=y_mean,
        x_mean=x_mean,
        x_std=x_std,
        train_n=len(y),
        train_rmse=train_rmse,
    )


def predict_ridge(model: RidgeFitResult, panel: pd.DataFrame) -> pd.Series:
    """Generate predictions for all rows (actuals + forecasts)."""
    x = panel[model.feature_names].to_numpy(dtype=np.float64)
    xz = (x - model.x_mean) / model.x_std
    yhat = model.intercept + xz @ model.coef
    return pd.Series(yhat, index=panel.index, name="rent_growth_predicted")


def rolling_backtest(
    panel: pd.DataFrame,
    target: str,
    feature_names: list[str],
    alphas: list[float] | None = None,
    train_min: int = 40,
    test_quarters: int = 40,
) -> dict:
    """Rolling 1-step-ahead backtest on actuals.

    Returns dict with rmse, mae, n, and per-quarter predictions.
    """
    actuals = panel[~panel["is_forecast"]].dropna(subset=[target]).copy()
    idx = actuals.index

    if len(idx) < train_min + 8:
        return {"rmse": float("nan"), "mae": float("nan"), "n": 0, "predictions": []}

    test_n = min(test_quarters, len(idx) - train_min)
    start_i = len(idx) - test_n

    y_true, y_pred, periods = [], [], []
    for i in range(start_i, len(idx)):
        train_slice = actuals.iloc[:i]
        if len(train_slice) < train_min:
            continue
        model = fit_ridge(train_slice, target, feature_names, alphas)
        if model is None:
            continue
        one = actuals.iloc[i:i + 1]
        pred = float(predict_ridge(model, one).iloc[0])
        true = float(one[target].iloc[0])
        if np.isfinite(pred) and np.isfinite(true):
            y_true.append(true)
            y_pred.append(pred)
            periods.append(str(one.index[0]))

    if not y_true:
        return {"rmse": float("nan"), "mae": float("nan"), "n": 0, "predictions": []}

    yt, yp = np.array(y_true), np.array(y_pred)
    rmse = float(np.sqrt(np.mean((yt - yp) ** 2)))
    mae = float(np.mean(np.abs(yt - yp)))

    predictions = [
        {"period": p, "actual": float(a), "predicted": float(pr)}
        for p, a, pr in zip(periods, y_true, y_pred)
    ]

    return {"rmse": rmse, "mae": mae, "n": len(yt), "predictions": predictions}


# ---------------------------------------------------------------------------
# Cascade model (4&5 Star → 3 Star → 1&2 Star)
# ---------------------------------------------------------------------------

@dataclass
class CascadeTierResult:
    tier: str
    model: RidgeFitResult | None
    backtest: dict
    feature_names: list[str]
    predictions: pd.Series | None


@dataclass
class CascadeResult:
    tiers: dict[str, CascadeTierResult]
    subject_tier: str
    subject_predictions: pd.Series | None
    subject_backtest: dict
    cascade_improves: bool  # Whether cascade beats standalone


# Core features available for any tier
_CORE_FEATURES = [
    "vacancy_rate", "uc_share", "deliveries_12mo_share", "absorption_12mo_share",
]

# Target column
_TARGET = "effective_rent_growth_yoy"


def _build_tier_features(panel: pd.DataFrame, extra_features: list[str] | None = None) -> list[str]:
    """Select available features from panel, adding lags."""
    features = []
    # Use lagged versions of core features to avoid leakage
    for f in _CORE_FEATURES:
        lag_name = f"{f}_lag1"
        if f in panel.columns:
            if lag_name not in panel.columns:
                panel[lag_name] = panel[f].shift(1)
            features.append(lag_name)

    # Add rent growth lag (autoregressive component)
    if _TARGET in panel.columns:
        lag_name = f"{_TARGET}_lag1"
        if lag_name not in panel.columns:
            panel[lag_name] = panel[_TARGET].shift(1)
        features.append(lag_name)

    # Add extra features (e.g., cascade inputs from higher tier)
    if extra_features:
        for f in extra_features:
            if f in panel.columns:
                features.append(f)

    # Drop features that are all NaN or constant
    valid = [f for f in features if f in panel.columns and panel[f].std() > 1e-10]
    return valid


def fit_cascade(
    df: pd.DataFrame,
    subject_star_rating: str = "1 & 2 Star",
    alphas: list[float] | None = None,
) -> CascadeResult:
    """Fit the cascade model: 4&5 Star → 3 Star → 1&2 Star.

    Each tier is modeled independently. The predicted rent growth from
    the higher tier feeds as an input feature to the next tier down.

    Args:
        df: Raw CoStar parquet DataFrame (all tiers).
        subject_star_rating: The tier you're underwriting.
        alphas: Ridge CV alpha candidates (None = default grid).

    Returns:
        CascadeResult with per-tier models, backtests, and predictions.
    """
    tiers = {}

    # --- Tier 1: 4&5 Star (top of cascade) ---
    panel_45 = build_submarket_panel(df, star_rating="4 & 5 Star")
    features_45 = _build_tier_features(panel_45)

    model_45 = fit_ridge(panel_45, _TARGET, features_45, alphas) if features_45 else None
    bt_45 = rolling_backtest(panel_45, _TARGET, features_45, alphas) if model_45 else {"rmse": float("nan"), "mae": float("nan"), "n": 0, "predictions": []}
    pred_45 = predict_ridge(model_45, panel_45) if model_45 else None

    tiers["4 & 5 Star"] = CascadeTierResult(
        tier="4 & 5 Star", model=model_45, backtest=bt_45,
        feature_names=features_45, predictions=pred_45,
    )

    # --- Tier 2: 3 Star (receives 4&5 Star cascade) ---
    panel_3 = build_submarket_panel(df, star_rating="3 Star")
    cascade_features_3 = []
    if pred_45 is not None:
        # Add 4&5 Star predicted rent growth as a lagged feature
        panel_3["cascade_45_rent_growth"] = pred_45.reindex(panel_3.index)
        panel_3["cascade_45_rent_growth_lag1"] = panel_3["cascade_45_rent_growth"].shift(1)
        cascade_features_3.append("cascade_45_rent_growth_lag1")
        # Also add 4&5 Star actual vacancy as context
        vac_45 = panel_45["vacancy_rate"].reindex(panel_3.index)
        panel_3["class_a_vacancy"] = vac_45
        panel_3["class_a_vacancy_lag1"] = panel_3["class_a_vacancy"].shift(1)
        cascade_features_3.append("class_a_vacancy_lag1")

    features_3 = _build_tier_features(panel_3, cascade_features_3)
    model_3 = fit_ridge(panel_3, _TARGET, features_3, alphas) if features_3 else None
    bt_3 = rolling_backtest(panel_3, _TARGET, features_3, alphas) if model_3 else {"rmse": float("nan"), "mae": float("nan"), "n": 0, "predictions": []}
    pred_3 = predict_ridge(model_3, panel_3) if model_3 else None

    tiers["3 Star"] = CascadeTierResult(
        tier="3 Star", model=model_3, backtest=bt_3,
        feature_names=features_3, predictions=pred_3,
    )

    # --- Tier 3: 1&2 Star (receives 3 Star + 4&5 Star cascade) ---
    panel_12 = build_submarket_panel(df, star_rating=subject_star_rating)
    cascade_features_12 = []

    if pred_3 is not None:
        panel_12["cascade_3_rent_growth"] = pred_3.reindex(panel_12.index)
        panel_12["cascade_3_rent_growth_lag1"] = panel_12["cascade_3_rent_growth"].shift(1)
        cascade_features_12.append("cascade_3_rent_growth_lag1")

    if pred_45 is not None:
        panel_12["cascade_45_rent_growth"] = pred_45.reindex(panel_12.index)
        panel_12["cascade_45_rent_growth_lag1"] = panel_12["cascade_45_rent_growth"].shift(1)
        cascade_features_12.append("cascade_45_rent_growth_lag1")
        vac_45 = panel_45["vacancy_rate"].reindex(panel_12.index)
        panel_12["class_a_vacancy"] = vac_45
        panel_12["class_a_vacancy_lag1"] = panel_12["class_a_vacancy"].shift(1)
        cascade_features_12.append("class_a_vacancy_lag1")

    features_12 = _build_tier_features(panel_12, cascade_features_12)
    model_12 = fit_ridge(panel_12, _TARGET, features_12, alphas) if features_12 else None
    bt_12 = rolling_backtest(panel_12, _TARGET, features_12, alphas) if model_12 else {"rmse": float("nan"), "mae": float("nan"), "n": 0, "predictions": []}
    pred_12 = predict_ridge(model_12, panel_12) if model_12 else None

    tiers[subject_star_rating] = CascadeTierResult(
        tier=subject_star_rating, model=model_12, backtest=bt_12,
        feature_names=features_12, predictions=pred_12,
    )

    # --- Compare cascade vs standalone ---
    standalone_features = _build_tier_features(
        build_submarket_panel(df, star_rating=subject_star_rating)
    )
    standalone_panel = build_submarket_panel(df, star_rating=subject_star_rating)
    _build_tier_features(standalone_panel)  # adds lag columns
    bt_standalone = rolling_backtest(standalone_panel, _TARGET, standalone_features, alphas)

    cascade_improves = (
        not np.isnan(bt_12.get("rmse", float("nan")))
        and not np.isnan(bt_standalone.get("rmse", float("nan")))
        and bt_12["rmse"] < bt_standalone["rmse"]
    )

    return CascadeResult(
        tiers=tiers,
        subject_tier=subject_star_rating,
        subject_predictions=pred_12,
        subject_backtest=bt_12,
        cascade_improves=cascade_improves,
    )


# ---------------------------------------------------------------------------
# Scenario generation (bull / base / bear)
# ---------------------------------------------------------------------------

@dataclass
class ScenarioForecast:
    scenario: str          # "bull", "base", "bear"
    quarters: list[str]    # period labels
    rent_growth: list[float]  # predicted YoY growth per quarter
    cumulative_growth: list[float]  # cumulative from current


def generate_scenarios(
    cascade: CascadeResult,
    panel: pd.DataFrame,
    horizon_quarters: int = 28,  # 7 years
) -> list[ScenarioForecast]:
    """Generate bull/base/bear scenarios calibrated to backtest RMSE.

    Base case = model prediction.
    Bull = base + 1 RMSE (market outperforms expectations).
    Bear = base - 1 RMSE (market underperforms).

    Args:
        cascade: CascadeResult from fit_cascade().
        panel: Subject tier panel with forecast quarters.
        horizon_quarters: Max quarters to forecast (default 28 = 7yr).

    Returns:
        List of 3 ScenarioForecast objects.
    """
    pred = cascade.subject_predictions
    bt = cascade.subject_backtest
    rmse = bt.get("rmse", 0.02)  # Default 2pp if no backtest

    if pred is None:
        return []

    # Use forecast quarters from the panel
    if "is_forecast" in panel.columns:
        forecast_mask = panel["is_forecast"]
    else:
        forecast_mask = pd.Series(False, index=panel.index)

    # Include latest actual + all forecast quarters
    actuals = panel[~forecast_mask]
    forecasts = panel[forecast_mask].head(horizon_quarters)

    if actuals.empty:
        return []

    forecast_periods = forecasts.index.tolist()
    if not forecast_periods:
        # If no CoStar forecast quarters, use recent actuals for demonstration
        return []

    scenarios = []
    for name, offset in [("bull", rmse), ("base", 0.0), ("bear", -rmse)]:
        growths = []
        cumulative = []
        cum = 1.0
        quarters = []

        for period in forecast_periods:
            if period in pred.index:
                base_growth = float(pred.loc[period])
            else:
                # Extrapolate: use the last predicted value
                base_growth = float(pred.iloc[-1]) if not pred.empty else 0.0

            adjusted = base_growth + offset
            growths.append(adjusted)
            cum *= (1 + adjusted / 4)  # Quarterly compounding approximation
            cumulative.append(cum - 1.0)
            quarters.append(str(period))

        scenarios.append(ScenarioForecast(
            scenario=name,
            quarters=quarters[:horizon_quarters],
            rent_growth=growths[:horizon_quarters],
            cumulative_growth=cumulative[:horizon_quarters],
        ))

    return scenarios
