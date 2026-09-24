"""Transform CoStar Market Analytics Excel export into per-submarket parquets.

Mirrors the schema of the existing DFW submarket parquets in
data/paid/costar-submarket/. One parquet per submarket, all star-rating slices
included (1 & 2 Star, 3 Star, 4 & 5 Star, All).

Usage:
    uv run python etl/transform_costar_excel.py \\
        --input data/paid/Birmingham_2026_Q2.xlsx \\
        --metro-slug birmingham_al_usa \\
        --out-dir data/paid/costar-submarket
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

import pandas as pd

# CoStar export column → DFW parquet column
COL_MAP: dict[str, str] = {
    "Period": "date",
    "Geography Name": "submarket",
    "Geography Code": "geography_code",
    "Slice": "star_rating",
    "Market Asking Rent/Unit": "asking_rent_unit",
    "Market Asking Rent/SF": "asking_rent_sf",
    "Market Asking Rent Growth": "asking_rent_growth_qoq",
    "Market Asking Rent Growth 12 Mo": "asking_rent_growth_yoy",
    "Market Asking Rent Index": "asking_rent_index",
    "Market Effective Rent/Unit": "effective_rent_unit",
    "Market Effective Rent/SF": "effective_rent_sf",
    "Market Effective Rent Growth": "effective_rent_growth_qoq",
    "Market Effective Rent Growth 12 Mo": "effective_rent_growth_yoy",
    "Vacancy Rate": "vacancy_rate",
    "Occupancy Rate": "occupancy_rate",
    "Stabilized Vacancy": "stabilized_vacancy",
    "Inventory Units": "inventory_units",
    "Under Construction Units": "under_construction",
    "Construction Starts Units": "construction_starts",
    "Construction Starts Units 12 Mo": "construction_starts_12mo",
    "Net Delivered Units": "deliveries",
    "Net Delivered Units 12 Mo": "deliveries_12mo",
    "Gross Delivered Buildings": "deliveries_gross",
    "Absorption Units": "absorption_units",
    "Absorption Units 12 Mo": "absorption_units_12mo",
    "Absorption %": "absorption_pct",
    "Demand Units": "demand_units",
    "Total Sales Volume": "sales_volume",
    "Total Sales Transactions": "sales_transactions",
    "Sold Units": "sold_units",
    "Cap Rate": "cap_rate",
    "Market Cap Rate": "market_cap_rate",
    "Market Asking Rent/Unit Studio": "asking_rent_studio",
    "Market Asking Rent/Unit 1 Bedroom": "asking_rent_1br",
    "Market Asking Rent/Unit 2 Bedroom": "asking_rent_2br",
    "Market Asking Rent/Unit 3 Bedroom": "asking_rent_3br",
    "Market Effective Rent/Unit Studio": "effective_rent_studio",
    "Market Effective Rent/Unit 1 Bedroom": "effective_rent_1br",
    "Market Effective Rent/Unit 2 Bedroom": "effective_rent_2br",
    "Market Effective Rent/Unit 3 Bedroom": "effective_rent_3br",
    "Forecast Scenario": "forecast_scenario",
}

# Final column order matching DFW reference parquets
OUTPUT_COLUMNS: list[str] = [
    "date", "submarket", "geography_code", "star_rating",
    "asking_rent_unit", "asking_rent_sf",
    "asking_rent_growth_qoq", "asking_rent_growth_yoy", "asking_rent_index",
    "effective_rent_unit", "effective_rent_sf",
    "effective_rent_growth_qoq", "effective_rent_growth_yoy",
    "vacancy_rate", "occupancy_rate", "stabilized_vacancy",
    "inventory_units", "under_construction",
    "construction_starts", "construction_starts_12mo",
    "deliveries", "deliveries_12mo", "deliveries_gross",
    "absorption_units", "absorption_units_12mo", "absorption_pct",
    "demand_units",
    "sales_volume", "sales_transactions", "sold_units",
    "cap_rate", "market_cap_rate",
    "asking_rent_studio", "asking_rent_1br", "asking_rent_2br", "asking_rent_3br",
    "effective_rent_studio", "effective_rent_1br", "effective_rent_2br", "effective_rent_3br",
    "forecast_scenario", "submarket_clean", "is_forecast",
]

INT_COLS = {"inventory_units", "deliveries_gross", "sales_transactions", "sold_units"}


def slugify_submarket(geography_name: str, metro_slug: str) -> str:
    """'Birmingham - AL USA - Downtown Birmingham' -> 'birmingham_al_usa_downtown_birmingham'."""
    parts = [p.strip() for p in geography_name.split("-")]
    sub = parts[-1] if parts else geography_name
    sub = re.sub(r"[^a-zA-Z0-9]+", "_", sub.lower()).strip("_")
    return f"{metro_slug}_{sub}"


def derive_metro_slug(geography_name: str) -> str:
    """'Dallas-Fort Worth - TX USA - Irving' -> 'dallas_fort_worth_tx_usa'.

    For statewide/multi-metro exports the metro is encoded in the geography name as
    'Metro - STATE USA - Submarket'. Combine the metro + 'STATE USA' parts so the
    resulting submarket slug matches the existing per-metro parquet naming
    (e.g. 'dallas_fort_worth_tx_usa_irving').
    """
    parts = [p.strip() for p in geography_name.split(" - ")]
    if len(parts) < 3:
        raise ValueError(f"Cannot derive metro from geography name: {geography_name!r}")
    metro_state = f"{parts[0]} {parts[1]}"
    return re.sub(r"[^a-z0-9]+", "_", metro_state.lower()).strip("_")


def quarter_to_sortable(q: str) -> tuple[int, int]:
    """'2026 Q2' or '2026 Q2 QTD'/'EST' -> (2026, 2). Trailing tokens ignored."""
    parts = q.split()
    return int(parts[0]), int(parts[1].lstrip("Q"))


def transform(df: pd.DataFrame, metro_slug: str, as_of: str) -> pd.DataFrame:
    """Rename + derive columns to match DFW parquet schema."""
    df = df.rename(columns=COL_MAP).copy()

    df["submarket_clean"] = df["submarket"].apply(lambda g: slugify_submarket(g, metro_slug))

    as_of_key = quarter_to_sortable(as_of)
    df["is_forecast"] = df["date"].apply(lambda p: quarter_to_sortable(p) > as_of_key)

    for col in OUTPUT_COLUMNS:
        if col not in df.columns:
            df[col] = pd.NA

    for col in INT_COLS:
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0).astype("int64")

    df = df[OUTPUT_COLUMNS]
    df = df.sort_values(["star_rating", "date"]).reset_index(drop=True)
    return df


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True, help="Path to CoStar export (.xlsx or .csv)")
    ap.add_argument("--metro-slug", default=None,
                    help="Metro slug prefix (e.g., birmingham_al_usa). Omit for "
                         "statewide/multi-metro exports — the metro is then derived "
                         "per-submarket from the geography name.")
    ap.add_argument("--out-dir", default="data/paid/costar-submarket")
    ap.add_argument("--sheet", default="DataExport")
    ap.add_argument("--index-name", default=None,
                    help="Basename for the written index CSV (default: <metro-slug>_index "
                         "or 'submarket_index' when auto-deriving).")
    args = ap.parse_args()

    in_path = Path(args.input)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"Reading {in_path}...")
    if in_path.suffix.lower() == ".csv":
        # utf-8-sig strips the BOM CoStar prepends to the header row.
        raw = pd.read_csv(in_path, encoding="utf-8-sig", low_memory=False)
    else:
        raw = pd.read_excel(in_path, sheet_name=args.sheet)
    print(f"  {len(raw)} rows, {len(raw.columns)} cols")

    raw = raw[raw["Geography Type"] == "Submarket"].copy()
    as_of = raw["As Of"].iloc[0]
    print(f"  As of: {as_of}")

    submarkets = sorted(raw["Geography Name"].unique())
    auto = args.metro_slug is None
    mode = "auto-derived per submarket" if auto else f"fixed '{args.metro_slug}'"
    print(f"  {len(submarkets)} submarkets found (metro slug: {mode})")

    written: list[str] = []
    metros: set[str] = set()
    for sub in submarkets:
        sub_df = raw[raw["Geography Name"] == sub].copy()
        metro_slug = derive_metro_slug(sub) if auto else args.metro_slug
        metros.add(metro_slug)
        out_df = transform(sub_df, metro_slug, as_of)
        slug = out_df["submarket_clean"].iloc[0]
        out_path = out_dir / f"{slug}.parquet"
        out_df.to_parquet(out_path, index=False)
        n_actual = (~out_df["is_forecast"]).sum()
        n_fcst = out_df["is_forecast"].sum()
        slices = sorted(out_df["star_rating"].unique())
        written.append(slug)
        print(f"  {slug}.parquet ({len(out_df)} rows: {n_actual} actual + {n_fcst} forecast; "
              f"slices={slices})")

    index_base = args.index_name or (f"{args.metro_slug}_index" if not auto else "submarket_index")
    index_path = out_dir / f"{index_base}.csv"
    pd.DataFrame({"slug": written}).to_csv(index_path, index=False)
    print(f"\nWrote index: {index_path}")
    print(f"Done — {len(written)} submarket parquets across {len(metros)} metro(s).")


if __name__ == "__main__":
    main()
