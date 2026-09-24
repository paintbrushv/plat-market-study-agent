"""Generate demographic visualization assets (maps + charts + infographic).

Called from render_demographics.py after the markdown report is generated.
Produces print-ready PNGs that can be embedded in the HTML report or used
in IC decks and lender packages.

Requires: geostack[print-maps,routing-multi]

Outputs (saved to reports/<metro>/<property>/demographics_assets/):
  - isochrone_map_mhi.png     — drive-time isochrone map, MHI choropleth
  - isochrone_map_rent.png    — drive-time isochrone map, rent choropleth
  - infographic_isochrone.png — demographic table by drive-time ring
  - affordability_curve.png   — % of HH that can afford each rent level
  - income_distribution.png   — HH income brackets by ring
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
from rich.console import Console

console = Console()


def generate_demographic_viz(
    config: dict,
    output_dir: Path,
) -> list[Path]:
    """Generate all demographic visualization assets for a property.

    Args:
        config: Full property YAML config dict.
        output_dir: Directory for output PNGs (e.g., reports/<metro>/<prop>/).

    Returns:
        List of generated file paths.
    """
    try:
        from geostack.viz_static import (
            broker_isochrone_map,
            demographic_infographic,
            save_map,
        )
    except ImportError:
        console.print("[yellow]Skipping viz — geostack[print-maps] not installed[/yellow]")
        return []

    import matplotlib.pyplot as plt

    subject = config["notes"]["subject_property"]
    demo_config = config["demographics"]
    metro_slug = config["notes"]["metro_slug"]

    lat = subject["lat"]
    lon = subject["lon"]
    property_name = subject["name"]
    avg_rent = subject.get("avg_rent", 0)
    post_reno_rent = subject.get("post_reno_rent", 0)
    rings = demo_config["drive_time_minutes"]
    state_fips = demo_config["state_fips"]
    county_fips = demo_config["county_fips"]
    acs_year = demo_config["acs_year"]

    # Load parquet data from ingest step
    data_dir = Path(f"data/public/processed/demographics/{metro_slug}")
    if not data_dir.exists():
        console.print(f"[yellow]No demographics data at {data_dir}[/yellow]")
        return []

    assets_dir = output_dir / "demographics_assets"
    assets_dir.mkdir(parents=True, exist_ok=True)
    generated = []

    # --- Load tract-level GeoDataFrames from parquets ---
    import geopandas as gpd

    # Find the largest ring's catchment parquet for the map background
    largest_ring = max(rings)
    catchment_file = data_dir / f"catchment_demographics_{largest_ring}min.parquet"

    if not catchment_file.exists():
        console.print(f"[yellow]No catchment file: {catchment_file.name}[/yellow]")
        return []

    tracts_gdf = gpd.read_parquet(catchment_file)
    console.print(f"[dim]Loaded {len(tracts_gdf)} tracts from {catchment_file.name}[/dim]")

    # --- Generate isochrone polygons ---
    try:
        from geostack.routing_multi import isochrones_nested

        iso_gdf = isochrones_nested(lon=lon, lat=lat, minutes=rings, provider="ors")
    except Exception as e:
        console.print(f"[yellow]Isochrone generation failed: {e}[/yellow]")
        iso_gdf = None

    # --- 1. Isochrone map — MHI ---
    if iso_gdf is not None and "median_household_income" in tracts_gdf.columns:
        try:
            fig, ax = broker_isochrone_map(
                tracts_gdf, iso_gdf,
                column="median_household_income",
                center=(lon, lat),
                property_name=property_name,
                subtitle=f"Drive-Time Isochrones — Median HHI (ACS {acs_year})",
                legend_title="Median HHI ($)",
            )
            path = save_map(fig, assets_dir / "isochrone_map_mhi.png", dpi=300)
            generated.append(path)
            plt.close(fig)
        except Exception as e:
            console.print(f"[yellow]Isochrone MHI map failed: {e}[/yellow]")

    # --- 2. Isochrone map — Median Rent ---
    if iso_gdf is not None and "median_rent" in tracts_gdf.columns:
        try:
            fig, ax = broker_isochrone_map(
                tracts_gdf, iso_gdf,
                column="median_rent",
                center=(lon, lat),
                property_name=property_name,
                subtitle=f"Drive-Time Isochrones — Median Rent (ACS {acs_year})",
                legend_title="Median Rent ($)",
                cmap="Oranges",
            )
            path = save_map(fig, assets_dir / "isochrone_map_rent.png", dpi=300)
            generated.append(path)
            plt.close(fig)
        except Exception as e:
            console.print(f"[yellow]Isochrone rent map failed: {e}[/yellow]")

    # --- 3. Demographic infographic by drive-time ring ---
    summary_file = data_dir / "demographics_summary.parquet"
    if summary_file.exists():
        try:
            summary = pd.read_parquet(summary_file)

            from geostack.viz_static import INFOGRAPHIC_VARIABLES
            from geostack.census_walker import isochrone_demographics

            if iso_gdf is not None:
                iso_stats = isochrone_demographics(
                    iso_gdf,
                    variables=INFOGRAPHIC_VARIABLES,
                    state=state_fips,
                    raw_codes=True,
                )
                fig, ax = demographic_infographic(
                    {m: dict(s) for m, s in iso_stats.items()},
                    radii_miles=rings,
                    property_name=property_name,
                    mode="isochrone",
                    subtitle=f"ACS {acs_year} | OpenRouteService Drive-Time",
                )
                path = save_map(fig, assets_dir / "infographic_isochrone.png", dpi=300)
                generated.append(path)
                plt.close(fig)
        except Exception as e:
            console.print(f"[yellow]Infographic failed: {e}[/yellow]")

    # --- 4. Affordability capture curve ---
    try:
        from geostack.affordability import (
            affordability_capture_curve,
            isochrone_renter_income,
        )
        from geostack.db import get_engine

        engine = get_engine()
        middle_ring = sorted(rings)[len(rings) // 2]  # Use middle ring

        renter_dist = isochrone_renter_income(
            lon, lat, minutes=middle_ring,
            state_fips=state_fips, county_fips=county_fips,
            year=acs_year, engine=engine,
        )
        curve = affordability_capture_curve(renter_dist)

        COLORS_BRAND = {"primary": "#1B3A5C", "text": "#333333"}
        fig, ax = plt.subplots(figsize=(12, 7), facecolor="white")

        rents = curve["monthly_rent"]
        pcts = curve["pct_can_afford"] * 100

        ax.fill_between(rents, pcts, alpha=0.15, color=COLORS_BRAND["primary"])
        ax.plot(rents, pcts, linewidth=2.5, color=COLORS_BRAND["primary"])

        # Mark property rents
        rent_markers = []
        if avg_rent:
            rent_markers.append((avg_rent, f"Current (${avg_rent:,.0f})", "#F57C00"))
        if post_reno_rent:
            rent_markers.append((post_reno_rent, f"Post-Reno (${post_reno_rent:,.0f})", "#D32F2F"))

        for rent, label, color in rent_markers:
            idx = (curve["monthly_rent"] - rent).abs().idxmin()
            pct = curve.loc[idx, "pct_can_afford"] * 100
            ax.axvline(rent, color=color, linestyle="--", alpha=0.7, linewidth=1.5)
            ax.annotate(
                f"{label}\n{pct:.0f}% can afford",
                xy=(rent, pct), xytext=(rent + 40, pct + 5),
                fontsize=9, fontweight="bold", color=color,
                arrowprops={"arrowstyle": "->", "color": color, "lw": 1.2},
                bbox={"boxstyle": "round,pad=0.3", "facecolor": "white",
                      "edgecolor": color, "alpha": 0.9},
            )

        ax.set_xlabel("Monthly Rent ($)", fontsize=12, color=COLORS_BRAND["text"])
        ax.set_ylabel("% of Renter HH That Can Afford", fontsize=12, color=COLORS_BRAND["text"])
        ax.set_title(
            f"{property_name} — Affordability Capture Curve ({middle_ring}-Min Drive)",
            fontsize=15, fontweight="bold", color=COLORS_BRAND["primary"], loc="left",
        )
        ax.set_xlim(800, 2000)
        ax.set_ylim(0, 100)
        ax.grid(axis="y", alpha=0.2)
        ax.spines["top"].set_visible(False)
        ax.spines["right"].set_visible(False)
        plt.tight_layout()

        path = save_map(fig, assets_dir / "affordability_curve.png", dpi=300)
        generated.append(path)
        plt.close(fig)
    except Exception as e:
        console.print(f"[yellow]Affordability curve failed: {e}[/yellow]")

    console.print(f"[green]Demographics viz: {len(generated)} assets generated[/green]")
    return generated
