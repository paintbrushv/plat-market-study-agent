"""Demographic & labor shed ETL for market study reports.

Reads a property YAML config, calls geostack demographic functions to
produce isochrone-based demographic profiles, employment analysis,
commute shed data, and affordability metrics.

Outputs parquet files to data/public/processed/demographics/<property_slug>/

Usage:
    uv run python etl/ingest_demographics.py --config agents/configs/dallas_tx_republics.yaml
    uv run python etl/ingest_demographics.py --config agents/configs/dallas_tx_republics.yaml --ring 15
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import pandas as pd
import yaml
from rich.console import Console
from rich.table import Table

console = Console()


def load_config(config_path: str) -> dict:
    """Load and validate a property YAML config."""
    with open(config_path) as f:
        config = yaml.safe_load(f)

    # Validate required fields
    subject = config.get("notes", {}).get("subject_property", {})
    demographics = config.get("demographics", {})

    if not subject.get("lat") or not subject.get("lon"):
        console.print("[red]Config missing subject_property lat/lon — required for isochrone analysis[/red]")
        sys.exit(1)

    if not demographics.get("state_fips") or not demographics.get("county_fips"):
        console.print("[red]Config missing demographics.state_fips / county_fips[/red]")
        sys.exit(1)

    return config


def preflight_check(state_fips: str, county_fips: str, lodes_year: int, acs_year: int) -> None:
    """Verify prerequisite data is loaded in PostGIS."""
    from geostack.db import get_engine, read_sql

    engine = get_engine()

    # Check census tracts
    tract_count = read_sql(
        "SELECT COUNT(*) AS n FROM census_tracts WHERE state_fips = :s AND county_fips = :c",
        engine=engine,
        params={"s": state_fips, "c": county_fips},
    ).iloc[0]["n"]

    if tract_count == 0:
        console.print(
            f"[red]No census tracts loaded for state={state_fips}, county={county_fips}. "
            f"Run: geostack download-tracts --state {state_fips} --year 2023 --load-db[/red]"
        )
        sys.exit(1)

    console.print(f"[dim]Preflight: {tract_count} tracts in {state_fips}/{county_fips}[/dim]")

    # Check ACS data
    acs_count = read_sql(
        "SELECT COUNT(*) AS n FROM acs_tract_data WHERE year = :y",
        engine=engine,
        params={"y": acs_year},
    ).iloc[0]["n"]

    if acs_count == 0:
        console.print(
            f"[yellow]Warning: No ACS data for year {acs_year}. "
            f"Run: geostack load-acs --state {state_fips} --county {county_fips} --year {acs_year}[/yellow]"
        )

    # Check LODES WAC data
    wac_count = read_sql(
        "SELECT COUNT(*) AS n FROM lodes_wac WHERE state_fips = :s AND county_fips = :c AND year = :y",
        engine=engine,
        params={"s": state_fips, "c": county_fips, "y": lodes_year},
    ).iloc[0]["n"]

    if wac_count == 0:
        console.print(
            f"[yellow]Warning: No LODES WAC data for {state_fips}/{county_fips} year {lodes_year}. "
            f"Run: geostack load-lodes --state {state_fips} --county {county_fips} --year {lodes_year}[/yellow]"
        )

    # Check LODES OD data
    od_count = read_sql(
        "SELECT COUNT(*) AS n FROM lodes_od WHERE w_state_fips = :s AND w_county_fips = :c AND year = :y",
        engine=engine,
        params={"s": state_fips, "c": county_fips, "y": lodes_year},
    ).iloc[0]["n"]

    if od_count == 0:
        console.print(
            f"[yellow]Warning: No LODES OD data for {state_fips}/{county_fips} year {lodes_year}. "
            f"Commute shed analysis will be empty.[/yellow]"
        )


def compute_affordability(
    demographics_summary: pd.DataFrame,
    avg_rent: float,
    property_name: str,
) -> pd.DataFrame:
    """Compute workforce-housing affordability metrics.

    Uses the 30% rent-to-income standard: a household can "afford" a unit
    if rent is ≤30% of gross monthly income.

    Args:
        demographics_summary: Summary row from isochrone_demographics.
        avg_rent: Average monthly rent at the subject property.
        property_name: Property name for the report.

    Returns:
        Single-row DataFrame with affordability metrics.
    """
    if demographics_summary.empty:
        return pd.DataFrame()

    row = demographics_summary.iloc[0]
    median_income = row.get("median_household_income", 0) or 0

    # Required annual income at 30% burden
    required_annual = avg_rent * 12 / 0.30 if avg_rent > 0 else 0

    # Affordability ratio: median income / required income
    affordability_ratio = median_income / required_annual if required_annual > 0 else 0

    # Can median-income household afford it?
    can_afford = affordability_ratio >= 1.0

    result = {
        "property_name": property_name,
        "avg_monthly_rent": avg_rent,
        "annual_rent": avg_rent * 12,
        "required_annual_income_30pct": round(required_annual),
        "catchment_median_hh_income": round(median_income),
        "affordability_ratio": round(affordability_ratio, 2),
        "median_hh_can_afford": can_afford,
        "minutes": row.get("minutes"),
    }

    return pd.DataFrame([result])


def run_demographics(config: dict, ring_filter: int | None = None) -> None:
    """Main demographics pipeline."""
    from geostack.demographics import (
        acs_time_series,
        clear_isochrone_cache,
        isochrone_commute_shed,
        isochrone_demographics,
        isochrone_employment,
        isochrone_income_distribution,
    )
    from geostack.qcew import top_employers_by_naics, wage_distribution

    clear_isochrone_cache()

    subject = config["notes"]["subject_property"]
    demo_config = config["demographics"]

    lat = subject["lat"]
    lon = subject["lon"]
    # Use ``or 0`` — explicit YAML null returns None from .get() default.
    avg_rent = subject.get("avg_rent") or 0
    property_name = subject["name"]
    metro_slug = config["notes"]["metro_slug"]

    state_fips = demo_config["state_fips"]
    county_fips = demo_config["county_fips"]
    acs_year = demo_config["acs_year"]
    lodes_year = demo_config["lodes_year"]
    qcew_year = demo_config["qcew_year"]
    drive_times = demo_config["drive_time_minutes"]
    trend_years = demo_config.get("trend_years", [2018, 2019, 2020, 2021, 2022, 2023])

    if ring_filter:
        drive_times = [ring_filter]

    # Output directory
    out_dir = Path(f"data/public/processed/demographics/{metro_slug}")
    out_dir.mkdir(parents=True, exist_ok=True)

    console.rule(f"[bold]Demographics & Labor Shed: {property_name}")
    console.print(f"Location: ({lat}, {lon})")
    console.print(f"Drive-time rings: {drive_times} min")
    console.print(f"Data years: ACS {acs_year}, LODES {lodes_year}, QCEW {qcew_year}")

    # Preflight
    preflight_check(state_fips, county_fips, lodes_year, acs_year)

    # --- Isochrone demographics for each ring ---
    demo_summaries = []
    for minutes in drive_times:
        console.rule(f"[bold]{minutes}-minute drive time")

        # Demographics
        tract_gdf, summary = isochrone_demographics(
            lon, lat, minutes, state_fips, county_fips, acs_year,
        )
        if not tract_gdf.empty:
            tract_gdf.to_parquet(out_dir / f"catchment_demographics_{minutes}min.parquet")

        if not summary.empty:
            demo_summaries.append(summary)

        # Employment
        emp_df, emp_summary = isochrone_employment(
            lon, lat, minutes, state_fips, county_fips, lodes_year,
        )
        if not emp_df.empty:
            emp_df.to_parquet(out_dir / f"catchment_employment_{minutes}min.parquet")

        if not emp_summary.empty:
            emp_summary.to_parquet(out_dir / f"employment_summary_{minutes}min.parquet")

    # --- Commute shed (use middle ring) ---
    shed_ring = drive_times[len(drive_times) // 2] if len(drive_times) > 1 else drive_times[0]
    console.rule(f"[bold]Commute shed ({shed_ring}-min ring)")

    shed_df = isochrone_commute_shed(
        lon, lat, shed_ring, state_fips, county_fips, lodes_year,
    )
    if not shed_df.empty:
        shed_df.to_parquet(out_dir / "commute_shed.parquet")

    # --- Income distribution, rent distribution, education ---
    console.rule("[bold]Income & rent distribution + education")
    try:
        income_df = isochrone_income_distribution(
            lon, lat, shed_ring, state_fips, county_fips, acs_year,
        )
        if not income_df.empty:
            # Save income brackets
            income_clean = income_df.copy()
            rent_dist = income_df.attrs.get("rent_distribution", {})
            education = income_df.attrs.get("education", {})
            income_clean.attrs = {}
            income_clean.to_parquet(out_dir / "income_distribution.parquet")

            # Save rent distribution
            if rent_dist:
                pd.DataFrame([rent_dist]).to_parquet(out_dir / "rent_distribution.parquet")

            # Save education
            if education:
                pd.DataFrame([education]).to_parquet(out_dir / "education.parquet")
    except Exception as e:
        console.print(f"[yellow]Income distribution failed: {e}[/yellow]")

    # --- QCEW wage data ---
    console.rule("[bold]QCEW employment & wages")
    try:
        top_sectors = top_employers_by_naics(state_fips, county_fips, qcew_year)
        top_sectors.to_parquet(out_dir / "qcew_top_sectors.parquet")

        wages = wage_distribution(state_fips, county_fips, qcew_year)
        # Extract attrs before parquet write (attrs don't serialize)
        wages_clean = wages.copy()
        wages_clean.attrs = {}
        wages_clean.to_parquet(out_dir / "qcew_wage_distribution.parquet")
    except Exception as e:
        console.print(f"[yellow]QCEW fetch failed: {e}[/yellow]")

    # --- ACS time series ---
    console.rule("[bold]ACS demographic trends")
    try:
        trends = acs_time_series(state_fips, county_fips, trend_years)

        # Extract CAGR before clearing attrs
        cagr_df = trends.attrs.get("cagr", pd.DataFrame()) if hasattr(trends, "attrs") else pd.DataFrame()

        trends_clean = trends.copy()
        trends_clean.attrs = {}
        trends_clean.to_parquet(out_dir / "acs_trends.parquet")

        if isinstance(cagr_df, pd.DataFrame) and not cagr_df.empty:
            cagr_df.to_parquet(out_dir / "acs_cagr.parquet")
    except Exception as e:
        console.print(f"[yellow]ACS time series failed: {e}[/yellow]")

    # --- Affordability ---
    console.rule("[bold]Workforce-housing affordability")
    if demo_summaries and avg_rent > 0:
        # Use the middle ring for affordability
        mid_idx = len(demo_summaries) // 2
        affordability = compute_affordability(demo_summaries[mid_idx], avg_rent, property_name)
        if not affordability.empty:
            affordability.to_parquet(out_dir / "affordability_summary.parquet")
            _print_affordability(affordability)

    # --- Combined summary ---
    if demo_summaries:
        combined = pd.concat(demo_summaries, ignore_index=True)
        combined.to_parquet(out_dir / "demographics_summary.parquet")

    console.rule("[bold green]Demographics pipeline complete")
    console.print(f"Output: {out_dir}/")


def _print_affordability(df: pd.DataFrame) -> None:
    """Pretty-print affordability results."""
    row = df.iloc[0]
    table = Table(title="Workforce-Housing Affordability")
    table.add_column("Metric", style="cyan")
    table.add_column("Value", style="white")

    table.add_row("Avg Monthly Rent", f"${row['avg_monthly_rent']:,.0f}")
    table.add_row("Required Income (30% burden)", f"${row['required_annual_income_30pct']:,.0f}/yr")
    table.add_row("Catchment Median HH Income", f"${row['catchment_median_hh_income']:,.0f}/yr")
    table.add_row("Affordability Ratio", f"{row['affordability_ratio']:.2f}")
    table.add_row(
        "Median HH Can Afford?",
        "[green]Yes[/green]" if row["median_hh_can_afford"] else "[red]No[/red]",
    )

    console.print(table)


def main() -> None:
    parser = argparse.ArgumentParser(description="Demographics & labor shed ETL")
    parser.add_argument(
        "--config", required=True,
        help="Path to property YAML config (e.g., agents/configs/dallas_tx_republics.yaml)",
    )
    parser.add_argument(
        "--ring", type=int, default=None,
        help="Run only a single drive-time ring (e.g., 15)",
    )
    args = parser.parse_args()

    config = load_config(args.config)
    run_demographics(config, ring_filter=args.ring)


if __name__ == "__main__":
    main()
