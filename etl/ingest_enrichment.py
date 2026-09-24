"""Deal enrichment ETL — calls geostack site-profile modules.

Runs environmental screening, zoning risk, demand projections, school quality,
transit access, migration flows, property tax, and flood risk for a deal's
location. Outputs a unified JSON file for rendering and downstream ingestion.

Usage:
    uv run python etl/ingest_enrichment.py --config agents/configs/dallas_tx_republics.yaml
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import yaml
from rich.console import Console

console = Console()

# Geostack imports are optional at module load time (require PostGIS).
# They are imported here so @patch decorators in tests can target
# etl.ingest_enrichment.screen_site / county_name_bulk.
try:
    from geostack.environmental import screen_site
    from geostack.fips import county_name_bulk
    from geostack.migration import migration_profile
    from geostack.projections import county_demand_forecast, tract_growth_profile
    from geostack.property_tax import effective_tax_rate
    from geostack.risk import flood_zone
    from geostack.schools import school_profile
    from geostack.transit import transit_profile
    from geostack.zoning import zoning_risk
except ImportError:
    screen_site = None  # type: ignore[assignment]
    county_name_bulk = None  # type: ignore[assignment]
    migration_profile = None  # type: ignore[assignment]
    county_demand_forecast = None  # type: ignore[assignment]
    tract_growth_profile = None  # type: ignore[assignment]
    effective_tax_rate = None  # type: ignore[assignment]
    flood_zone = None  # type: ignore[assignment]
    school_profile = None  # type: ignore[assignment]
    transit_profile = None  # type: ignore[assignment]
    zoning_risk = None  # type: ignore[assignment]


def load_config(config_path: str) -> dict:
    """Load and validate a property YAML config for enrichment."""
    with open(config_path) as f:
        config = yaml.safe_load(f)

    subject = config.get("notes", {}).get("subject_property", {})
    demographics = config.get("demographics", {})

    if not subject.get("lat") or not subject.get("lon"):
        console.print(
            "[red]Config missing subject_property lat/lon — required for enrichment[/red]"
        )
        sys.exit(1)

    if not demographics.get("state_fips") or not demographics.get("county_fips"):
        console.print("[red]Config missing demographics.state_fips / county_fips[/red]")
        sys.exit(1)

    return config


def _parse_target_year(time_horizon: str) -> int:
    """Extract end year from time_horizon string (e.g., '2025-2030' -> 2030)."""
    parts = time_horizon.strip().split("-")
    for part in reversed(parts):
        part = part.strip()
        if part.isdigit() and len(part) == 4:
            return int(part)
    return 2030


def _run_module(name: str, fn: Callable, **kwargs: object) -> dict:
    """Run a geostack module with error handling."""
    try:
        data = fn(**kwargs)
        return {"status": "ok", "data": data}
    except Exception as e:
        console.print(f"[yellow]⚠ {name}: {e}[/yellow]")
        return {"status": "error", "error": str(e)}


def run_enrichment(config: dict) -> dict:
    """Run all enrichment modules and return the result dict."""
    subject = config["notes"]["subject_property"]
    demo_config = config["demographics"]

    lat = subject["lat"]
    lon = subject["lon"]
    state_fips = str(demo_config["state_fips"])
    county_fips = str(demo_config["county_fips"])
    address = subject["address"]
    property_name = subject["name"]
    target_year = _parse_target_year(config.get("time_horizon", "2025-2030"))

    # Derive county name from FIPS. ``county_name_bulk`` returns a dict
    # keyed by concatenated state+county FIPS (e.g. ``"48113"``), matching
    # the access pattern used in render_demographics.py:164.
    county_names = county_name_bulk([(state_fips, county_fips)])
    county_name = county_names.get(
        f"{state_fips}{county_fips}", f"{state_fips}/{county_fips}"
    )

    console.rule(f"[bold]Site Enrichment: {property_name}")
    console.print(f"Location: ({lat}, {lon}) — {address}")
    console.print(f"County: {county_name} ({state_fips}/{county_fips})")
    console.print(f"Target year: {target_year}")

    walkscore_key = os.environ.get("WALKSCORE_API_KEY")

    modules: dict = {}

    # --- Environmental ---
    console.print("[bold]Running: Environmental screening...[/bold]")
    modules["environmental"] = _run_module(
        "environmental",
        screen_site,
        lat=lat,
        lon=lon,
        radius_miles=1.0,
    )

    # --- Zoning ---
    console.print("[bold]Running: Zoning risk...[/bold]")
    modules["zoning"] = _run_module(
        "zoning",
        zoning_risk,
        lat=lat,
        lon=lon,
        radius_miles=1.0,
    )

    # --- Projections (two calls, one key) ---
    console.print("[bold]Running: Demand projections...[/bold]")
    county_demand = _run_module(
        "projections.county_demand",
        county_demand_forecast,
        state_fips=state_fips,
        county_fips=county_fips,
        target_year=target_year,
    )
    tract_growth = _run_module(
        "projections.tract_growth",
        tract_growth_profile,
        state_fips=state_fips,
        county_fips=county_fips,
    )
    # Convert DataFrame to records if successful
    if tract_growth["status"] == "ok" and hasattr(tract_growth["data"], "to_dict"):
        tract_growth["data"] = tract_growth["data"].to_dict(orient="records")

    # Merge into single projections entry
    if county_demand["status"] == "ok" or tract_growth["status"] == "ok":
        modules["projections"] = {
            "status": "ok",
            "data": {
                "county_demand": (
                    county_demand.get("data") if county_demand["status"] == "ok" else None
                ),
                "tract_growth": (
                    tract_growth.get("data") if tract_growth["status"] == "ok" else None
                ),
            },
        }
    else:
        modules["projections"] = {
            "status": "error",
            "error": f"county: {county_demand.get('error')}; tract: {tract_growth.get('error')}",
        }

    # --- Schools ---
    console.print("[bold]Running: School profile...[/bold]")
    modules["schools"] = _run_module(
        "schools",
        school_profile,
        lon=lon,
        lat=lat,
    )

    # --- Transit ---
    console.print("[bold]Running: Transit profile...[/bold]")
    modules["transit"] = _run_module(
        "transit",
        transit_profile,
        lon=lon,
        lat=lat,
        address=address,
        api_key=walkscore_key,
    )

    # --- Migration ---
    console.print("[bold]Running: Migration profile...[/bold]")
    modules["migration"] = _run_module(
        "migration",
        migration_profile,
        state_fips=state_fips,
        county_fips=county_fips,
    )

    # --- Property Tax ---
    console.print("[bold]Running: Property tax...[/bold]")
    modules["property_tax"] = _run_module(
        "property_tax",
        effective_tax_rate,
        lon=lon,
        lat=lat,
        county_name=county_name,
    )

    # --- Flood Risk ---
    console.print("[bold]Running: Flood risk...[/bold]")
    modules["flood_risk"] = _run_module(
        "flood_risk",
        flood_zone,
        lon=lon,
        lat=lat,
    )

    result = {
        "property": property_name,
        "address": address,
        "lat": lat,
        "lon": lon,
        "state_fips": state_fips,
        "county_fips": county_fips,
        "county_name": county_name,
        "target_year": target_year,
        "generated_at": datetime.now(tz=UTC).isoformat(),
        "modules": modules,
    }

    return result


def write_enrichment(result: dict, metro_slug: str) -> Path:
    """Write enrichment result to JSON."""
    out_dir = Path(f"data/public/processed/enrichment/{metro_slug}")
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "site_enrichment.json"
    with open(out_path, "w") as f:
        json.dump(result, f, indent=2, default=str)
    console.print(f"[green]Wrote: {out_path}[/green]")
    return out_path


def main() -> None:
    """Run the deal enrichment pipeline from the command line."""
    parser = argparse.ArgumentParser(description="Deal enrichment ETL")
    parser.add_argument(
        "--config",
        required=True,
        help="Path to property YAML config",
    )
    args = parser.parse_args()

    config = load_config(args.config)
    result = run_enrichment(config)
    metro_slug = config["notes"]["metro_slug"]
    write_enrichment(result, metro_slug)
    console.rule("[bold green]Enrichment pipeline complete")


if __name__ == "__main__":
    main()
