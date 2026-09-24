"""Build consolidated DFW CoStar panel parquet from per-submarket parquets.

Melts the 39 per-submarket DFW parquets in data/paid/costar-submarket-v1/
into one long-form panel matching the schema of
data/paid/processed/costar_panel_submarket.parquet so viz scripts can port cleanly.

Output columns:
    metro, submarket, geography_code, property_type, star_rating,
    period, period_ts, is_forecast, metric, value
"""
from __future__ import annotations

import re
from pathlib import Path

import pandas as pd

SRC_DIR = Path("data/paid/costar-submarket-v1")
OUT = Path("data/paid/processed/costar_panel_submarket_dfw.parquet")
METRO = "Dallas-FW"

# columns → exported CoStar metric names (long-form labels used by viz scripts)
COL_TO_METRIC: dict[str, str] = {
    "asking_rent_unit": "Market Asking Rent/Unit",
    "asking_rent_sf": "Market Asking Rent/SF",
    "asking_rent_growth_qoq": "Market Asking Rent Growth",
    "asking_rent_growth_yoy": "Market Asking Rent Growth 12 Mo",
    "asking_rent_index": "Market Asking Rent Index",
    "effective_rent_unit": "Market Effective Rent/Unit",
    "effective_rent_sf": "Market Effective Rent/SF",
    "effective_rent_growth_qoq": "Market Effective Rent Growth",
    "effective_rent_growth_yoy": "Market Effective Rent Growth 12 Mo",
    "vacancy_rate": "Vacancy Rate",
    "occupancy_rate": "Occupancy Rate",
    "stabilized_vacancy": "Stabilized Vacancy",
    "inventory_units": "Inventory Units",
    "under_construction": "Under Construction Units",
    "construction_starts": "Construction Starts Units",
    "construction_starts_12mo": "Construction Starts Units 12 Mo",
    "deliveries": "Net Delivered Units",
    "deliveries_12mo": "Net Delivered Units 12 Mo",
    "absorption_units": "Absorption Units",
    "absorption_units_12mo": "Absorption Units 12 Mo",
    "absorption_pct": "Absorption %",
    "demand_units": "Demand Units",
    "sales_volume": "Total Sales Volume",
    "sales_transactions": "Total Sales Transactions",
    "sold_units": "Sold Units",
    "cap_rate": "Cap Rate",
    "market_cap_rate": "Market Cap Rate",
    "asking_rent_studio": "Market Asking Rent/Unit Studio",
    "asking_rent_1br": "Market Asking Rent/Unit 1 Bedroom",
    "asking_rent_2br": "Market Asking Rent/Unit 2 Bedroom",
    "asking_rent_3br": "Market Asking Rent/Unit 3 Bedroom",
}


def parse_period(p: str) -> pd.Timestamp:
    m = re.match(r"(\d{4})\s*Q([1-4])", str(p))
    if not m:
        return pd.NaT
    year, q = int(m.group(1)), int(m.group(2))
    month_end = {1: "03-31", 2: "06-30", 3: "09-30", 4: "12-31"}[q]
    return pd.Timestamp(f"{year}-{month_end}")


def unslug_submarket(clean: str) -> str:
    """'dallas_fort_worth_tx_usa_far_north_dallas' -> 'Far North Dallas'."""
    sub = clean.replace("dallas_fort_worth_tx_usa_", "")
    # Title-case and fix common multi-word names.
    parts = sub.split("_")
    # Special cases: preserve slashes for multi-word submarkets
    name = " ".join(p.capitalize() for p in parts)
    # Fix common CoStar naming
    name = name.replace("Mckinney", "McKinney").replace("Dfw", "DFW")
    return name


def main() -> None:
    parquets = sorted(p for p in SRC_DIR.glob("dallas_fort_worth_tx_usa_*.parquet"))
    print(f"Reading {len(parquets)} DFW submarket parquets...")

    frames: list[pd.DataFrame] = []
    for path in parquets:
        df = pd.read_parquet(path)
        if df.empty:
            continue
        df["period_ts"] = df["date"].apply(parse_period)
        df["submarket"] = df["submarket_clean"].apply(unslug_submarket)

        id_cols = ["submarket", "geography_code", "star_rating", "date", "period_ts", "is_forecast"]
        value_cols = [c for c in COL_TO_METRIC if c in df.columns]

        long_df = df[id_cols + value_cols].melt(
            id_vars=id_cols,
            value_vars=value_cols,
            var_name="metric_raw",
            value_name="value",
        )
        long_df["metric"] = long_df["metric_raw"].map(COL_TO_METRIC)
        long_df = long_df.drop(columns=["metric_raw"])
        long_df = long_df.dropna(subset=["value"])
        frames.append(long_df)

    panel = pd.concat(frames, ignore_index=True)
    panel["metro"] = METRO
    panel["property_type"] = "Multifamily"
    panel = panel.rename(columns={"date": "period"})
    panel = panel[[
        "metro", "submarket", "geography_code", "property_type", "star_rating",
        "period", "period_ts", "is_forecast", "metric", "value",
    ]]

    OUT.parent.mkdir(parents=True, exist_ok=True)
    panel.to_parquet(OUT, index=False)

    print(f"Wrote {OUT}")
    print(f"  {len(panel):,} rows")
    print(f"  Submarkets: {panel['submarket'].nunique()}")
    print(f"  Star slices: {sorted(panel['star_rating'].unique())}")
    print(f"  Metrics: {panel['metric'].nunique()}")
    print(f"  Period range: {panel['period_ts'].min()} → {panel['period_ts'].max()}")


if __name__ == "__main__":
    main()
