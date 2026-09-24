"""
etl/load_costar_submarket.py — Load and format CoStar submarket fundamentals.

Usage (CLI):
    uv run python etl/load_costar_submarket.py --config agents/configs/dallas_tx_republics.yaml
    uv run python etl/load_costar_submarket.py --config agents/configs/dallas_tx_republics.yaml --star-rating "All"
    uv run python etl/load_costar_submarket.py --parquet data/paid/costar-submarket/dallasfort_worth_tx_usa_garlandrowlett.parquet

Programmatic use:
    from etl.load_costar_submarket import load_submarket, get_recent_actuals, format_submarket_table
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

import pandas as pd
import yaml

# Ordered star ratings for display
STAR_RATINGS_ORDER = ["1 & 2 Star", "3 Star", "4 & 5 Star", "All"]

DISPLAY_COLS = {
    "date": "Period",
    "asking_rent_unit": "Ask Rent",
    "effective_rent_unit": "Eff Rent",
    "asking_rent_1br": "Ask 1BR",
    "asking_rent_2br": "Ask 2BR",
    "effective_rent_1br": "Eff 1BR",
    "effective_rent_2br": "Eff 2BR",
    "vacancy_rate": "Vacancy",
    "asking_rent_growth_yoy": "Rent Gro YoY",
    "deliveries_12mo": "Deliver 12mo",
    "absorption_units_12mo": "Absorb 12mo",
    "under_construction": "Under Con",
}


def load_submarket(parquet_path: str | Path, star_rating: str | None = None) -> pd.DataFrame:
    """
    Load a submarket parquet file, optionally filtered to one star-rating tier.

    Parameters
    ----------
    parquet_path : path to the .parquet file
    star_rating  : one of "1 & 2 Star", "3 Star", "4 & 5 Star", "All" — or None for all rows
    """
    path = Path(parquet_path)
    if not path.exists():
        raise FileNotFoundError(f"Submarket parquet not found: {path}")

    df = pd.read_parquet(path)
    if star_rating:
        df = df[df["star_rating"] == star_rating].copy()
    df = df.sort_values(["star_rating", "date"])
    return df


def get_recent_actuals(df: pd.DataFrame, n_quarters: int = 8) -> pd.DataFrame:
    """Return the most recent n_quarters of actual (non-forecast) rows."""
    actual = df[~df["is_forecast"]].copy()
    # Deduplicate: prefer non-EST rows when a QTD/EST duplicate exists
    # CoStar sometimes has both "2026 Q1 EST" and "2026 Q1 QTD" for the current quarter
    # Keep the most informative one (QTD has finer data)
    # Group by (star_rating, base_quarter) where base_quarter = date.split()[0:2]
    actual["_base_q"] = actual["date"].str.replace(r"\s+(EST|QTD)$", "", regex=True)
    actual = (
        actual.sort_values("date")
        .drop_duplicates(subset=["star_rating", "_base_q"], keep="last")
        .drop(columns=["_base_q"])
    )
    return actual.groupby("star_rating", group_keys=False).tail(n_quarters)


def get_forecast(df: pd.DataFrame, n_quarters: int = 8) -> pd.DataFrame:
    """Return the next n_quarters of forecasted rows."""
    return df[df["is_forecast"]].groupby("star_rating", group_keys=False).head(n_quarters)


def format_submarket_table(
    df: pd.DataFrame,
    star_ratings: list[str] | None = None,
    n_quarters: int = 6,
    include_forecast: bool = False,
) -> str:
    """
    Format a Markdown submarket fundamentals table.

    Parameters
    ----------
    df           : DataFrame from load_submarket()
    star_ratings : tiers to include; None = all present
    n_quarters   : recent actuals to show
    include_forecast : append next 4 forecast quarters
    """
    tiers = star_ratings or [r for r in STAR_RATINGS_ORDER if r in df["star_rating"].unique()]
    sections: list[str] = []

    for tier in tiers:
        tier_df = df[df["star_rating"] == tier]
        rows = get_recent_actuals(tier_df, n_quarters)
        if include_forecast:
            fcast = get_forecast(tier_df, 4)
            rows = pd.concat([rows, fcast])

        if rows.empty:
            continue

        # Build display rows
        records: list[dict[str, Any]] = []
        for _, row in rows.iterrows():
            period = row["date"]
            if row.get("is_forecast", False):
                period = f"*{period}*"
            rec: dict[str, Any] = {"Period": period}
            rec["Ask Rent"] = f"${row['asking_rent_unit']:,.0f}" if pd.notna(row["asking_rent_unit"]) else "—"
            rec["Eff Rent"] = f"${row['effective_rent_unit']:,.0f}" if pd.notna(row["effective_rent_unit"]) else "—"
            rec["Ask 1BR"] = f"${row['asking_rent_1br']:,.0f}" if pd.notna(row["asking_rent_1br"]) else "—"
            rec["Ask 2BR"] = f"${row['asking_rent_2br']:,.0f}" if pd.notna(row["asking_rent_2br"]) else "—"
            rec["Vacancy"] = f"{row['vacancy_rate']:.1%}" if pd.notna(row["vacancy_rate"]) else "—"
            rec["Rent Gro YoY"] = (
                f"{row['asking_rent_growth_yoy']:+.1%}" if pd.notna(row["asking_rent_growth_yoy"]) else "—"
            )
            rec["Deliver 12mo"] = (
                f"{int(row['deliveries_12mo'])}" if pd.notna(row["deliveries_12mo"]) else "—"
            )
            rec["Absorb 12mo"] = (
                f"{int(row['absorption_units_12mo'])}" if pd.notna(row["absorption_units_12mo"]) else "—"
            )
            rec["Under Con"] = (
                f"{int(row['under_construction'])}" if pd.notna(row["under_construction"]) else "—"
            )
            records.append(rec)

        display_df = pd.DataFrame(records)
        headers = list(display_df.columns)
        sep = [":---"] + ["---:"] * (len(headers) - 1)

        lines = [
            f"### {tier}",
            "",
            "| " + " | ".join(headers) + " |",
            "| " + " | ".join(sep) + " |",
        ]
        for _, row in display_df.iterrows():
            lines.append("| " + " | ".join(str(v) for v in row.values) + " |")
        sections.append("\n".join(lines))

    return "\n\n".join(sections)


def submarket_snapshot(
    parquet_path: str | Path,
    star_rating: str | None = None,
    n_quarters: int = 6,
    include_forecast: bool = False,
) -> str:
    """
    One-shot: load a parquet and return formatted Markdown tables.
    If star_rating is None, prints all four tiers.
    """
    df = load_submarket(parquet_path, star_rating)
    tiers = [star_rating] if star_rating else None
    return format_submarket_table(df, tiers, n_quarters=n_quarters, include_forecast=include_forecast)


def _load_config(config_path: str) -> dict[str, Any]:
    with open(config_path) as f:
        return yaml.safe_load(f)


def main() -> None:
    parser = argparse.ArgumentParser(description="Print CoStar submarket fundamentals table")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--config", help="Path to property YAML config")
    group.add_argument("--parquet", help="Direct path to submarket parquet file")
    parser.add_argument(
        "--star-rating",
        default=None,
        choices=["1 & 2 Star", "3 Star", "4 & 5 Star", "All"],
        help="Filter to a single star-rating tier (default: all tiers)",
    )
    parser.add_argument("--quarters", type=int, default=8, help="Recent actual quarters to show (default: 8)")
    parser.add_argument("--forecast", action="store_true", help="Append next 4 forecast quarters")
    args = parser.parse_args()

    if args.config:
        cfg = _load_config(args.config)
        sub = cfg.get("costar_submarket")
        if not sub:
            print(f"ERROR: no 'costar_submarket' key in {args.config}", file=sys.stderr)
            sys.exit(1)
        parquet_path = sub["parquet"]
        star_rating = args.star_rating or sub.get("primary_star_rating")
    else:
        parquet_path = args.parquet
        star_rating = args.star_rating

    try:
        output = submarket_snapshot(
            parquet_path,
            star_rating=star_rating,
            n_quarters=args.quarters,
            include_forecast=args.forecast,
        )
        print(output)
    except FileNotFoundError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
