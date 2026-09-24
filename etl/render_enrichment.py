# etl/render_enrichment.py
"""Render site profile markdown report from enrichment JSON.

Reads the site_enrichment.json produced by ingest_enrichment.py and generates
a site profile report with environmental, zoning, projections, schools, transit,
migration, property tax, and flood risk sections.

Usage:
    uv run python etl/render_enrichment.py --config agents/configs/dallas_tx_republics.yaml
    uv run python etl/render_enrichment.py --config agents/configs/dallas_tx_republics.yaml --html
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

import yaml
from rich.console import Console

console = Console()


def _fmt_num(val: object, prefix: str = "", suffix: str = "", decimals: int = 0) -> str:
    """Format a number with commas, optional prefix/suffix."""
    if val is None:
        return "\u2014"
    if decimals == 0:
        return f"{prefix}{int(val):,}{suffix}"
    return f"{prefix}{val:,.{decimals}f}{suffix}"


def _fmt_pct(val: object, decimals: int = 1) -> str:
    if val is None:
        return "\u2014"
    return f"{val * 100:.{decimals}f}%"


def _section_unavailable(title: str, error: str) -> str:
    return f"## {title}\n\n> \u26a0 Data unavailable: {error}\n"


def _render_environmental(data: dict) -> str:
    lines = ["## Environmental Screening\n"]
    summary = data.get("summary", {})
    risk_flags = data.get("risk_flags", [])

    echo_count = summary.get("echo_facility_count", 0) if isinstance(summary, dict) else 0
    cleanup_count = summary.get("cleanup_site_count", 0) if isinstance(summary, dict) else 0

    lines.append("| Metric | Value | Flag |")
    lines.append("| --- | ---: | :---: |")
    lines.append(f"| EPA ECHO Facilities (1 mi) | {echo_count} | \u2014 |")

    cleanup_flag = "**\u26a0**" if cleanup_count > 0 else "\u2713"
    lines.append(f"| Cleanup Sites (CIMC) | {cleanup_count} | {cleanup_flag} |")

    flag_str = ", ".join(risk_flags) if risk_flags else "None"
    flag_icon = "**\u26a0**" if risk_flags else "\u2713"
    lines.append(f"| Risk Flags | {flag_str} | {flag_icon} |")

    return "\n".join(lines) + "\n"


def _render_zoning(data: dict) -> str:
    lines = ["## Zoning & Supply Threat\n"]
    subject_zoning = data.get("subject_zoning", {})
    summary = data.get("summary", {})
    city = data.get("city", "\u2014")

    em = "\u2014"
    zoning_code = subject_zoning.get("code", em) if isinstance(subject_zoning, dict) else em
    supply_threat = summary.get("supply_threat", em) if isinstance(summary, dict) else em
    quality_threat = summary.get("quality_threat", em) if isinstance(summary, dict) else em

    lines.append("| Metric | Value |")
    lines.append("| --- | --- |")
    lines.append(f"| Subject Zoning | {zoning_code} |")
    lines.append(f"| City | {city} |")

    supply_str = f"**{supply_threat}**" if supply_threat == "High" else supply_threat
    lines.append(f"| Supply Threat | {supply_str} |")
    lines.append(f"| Quality Threat | {quality_threat} |")

    return "\n".join(lines) + "\n"


def _render_projections(data: dict, target_year: int) -> str:
    lines = [f"## Demand Projections ({target_year})\n"]

    county = data.get("county_demand")
    if county:
        lines.append("| Indicator | Base Year | Projected (Base) | CAGR |")
        lines.append("| --- | ---: | ---: | ---: |")

        for key in ["population", "employment", "households"]:
            section = county.get(key, {})
            if section:
                base = _fmt_num(section.get("base_value"))
                # ``projected`` can be present but ``None`` (e.g. when the
                # employment series has insufficient history to project) — use
                # ``or {}`` rather than the dict-default to avoid
                # NoneType.get errors.
                projected = _fmt_num((section.get("projected") or {}).get("base"))
                cagr = _fmt_pct(section.get("cagr"))
                lines.append(f"| {key.title()} | {base} | {projected} | {cagr} |")

        lines.append("")

    tract_growth = data.get("tract_growth", [])
    if tract_growth:
        counts = Counter(t.get("classification", "Unknown") for t in tract_growth)
        total = len(tract_growth)
        dist_parts = []
        for cls, cnt in counts.most_common():
            pct = cnt / total * 100
            dist_parts.append(f"{pct:.0f}% {cls}")
        dist_str = " | ".join(dist_parts)
        lines.append(f"Tract Growth Distribution: {dist_str}\n")

    return "\n".join(lines) + "\n"


def _render_schools(data: dict) -> str:
    lines = ["## School Quality\n"]
    em = "\u2014"
    lines.append("| Metric | Value |")
    lines.append("| --- | --- |")
    lines.append(f"| District | {data.get('district_name', em)} |")
    lines.append(f"| TEA Rating | {data.get('tea_rating', em)} |")

    if data.get("student_achievement") is not None:
        lines.append(f"| Student Achievement | {data['student_achievement']} |")
    if data.get("school_progress") is not None:
        lines.append(f"| School Progress | {data['school_progress']} |")
    if data.get("closing_gaps") is not None:
        lines.append(f"| Closing Gaps | {data['closing_gaps']} |")

    return "\n".join(lines) + "\n"


def _render_transit(data: dict) -> str:
    lines = ["## Transit Access\n"]
    em = "\u2014"
    lines.append("| Score | Value |")
    lines.append("| --- | ---: |")
    lines.append(f"| Walk Score | {data.get('walkscore', em)} |")
    lines.append(f"| Transit Score | {data.get('transit_score', em)} |")
    lines.append(f"| Bike Score | {data.get('bike_score', em)} |")

    nearest = data.get("nearest_rail")
    dist = data.get("nearest_rail_dist_mi")
    if nearest:
        dist_str = f" \u2014 {dist:.1f} mi" if dist else ""
        lines.append(f"| Nearest Rail | {nearest}{dist_str} |")

    return "\n".join(lines) + "\n"


def _render_migration(data: dict) -> str:
    lines = ["## Migration Flows\n"]
    soi = data.get("soi", {})

    net = soi.get("net_returns")
    net_prefix = "+" if (net or 0) >= 0 else ""

    lines.append("| Metric | Value |")
    lines.append("| --- | ---: |")
    lines.append(f"| Net Migration (IRS SOI) | {_fmt_num(net, prefix=net_prefix)} returns |")
    lines.append(f"| Inbound Avg AGI | {_fmt_num(soi.get('in_avg_agi'), prefix='$')} |")
    lines.append(f"| Outbound Avg AGI | {_fmt_num(soi.get('out_avg_agi'), prefix='$')} |")

    delta = soi.get("income_delta")
    if delta is not None:
        sign = "+" if delta >= 0 else ""
        qualifier = "positive selection" if delta >= 0 else "negative selection"
        lines.append(f"| Income Delta | {sign}{_fmt_num(abs(delta), prefix='$')} ({qualifier}) |")

    return "\n".join(lines) + "\n"


def _render_property_tax(data: dict) -> str:
    lines = ["## Property Tax\n"]
    lines.append("| Component | Rate |")
    lines.append("| --- | ---: |")

    county_rate = data.get("county_rate")
    if county_rate is not None:
        lines.append(f"| County | {_fmt_pct(county_rate, decimals=2)} |")

    school_dist = data.get("school_district", "")
    school_rate = data.get("school_rate")
    if school_rate is not None:
        label = f"School District ({school_dist})" if school_dist else "School District"
        lines.append(f"| {label} | {_fmt_pct(school_rate, decimals=2)} |")

    rate_per_100 = data.get("rate_per_100")
    if rate_per_100 is not None:
        lines.append(f"| **Total** | **{rate_per_100:.2f} per $100** |")

    return "\n".join(lines) + "\n"


def _render_flood_risk(data: dict) -> str:
    lines = ["## Flood Risk\n"]
    zone = data.get("zone", "\u2014")
    sfha = data.get("sfha", False)
    subtype = data.get("zone_subtype", "\u2014")

    sfha_str = "**Yes**" if sfha else "No"
    zone_str = f"**{zone}**" if sfha else zone

    lines.append("| Zone | SFHA | Detail |")
    lines.append("| :---: | :---: | --- |")
    lines.append(f"| {zone_str} | {sfha_str} | {subtype} |")

    if sfha:
        lines.append("")
        lines.append(
            "> **\u26a0 Property is in a Special Flood Hazard Area.** "
            "Federal flood insurance required for federally-backed mortgages."
        )

    return "\n".join(lines) + "\n"


def render_report(enrichment: dict) -> str:
    """Render the site profile report as markdown."""
    property_name = enrichment.get("property", "Unknown")
    address = enrichment.get("address", "")
    generated = enrichment.get("generated_at", "")
    target_year = enrichment.get("target_year", 2030)
    modules = enrichment.get("modules", {})

    lines = [
        f"# Site Profile \u2014 {property_name}\n",
        f"> Generated {generated[:10]} | {address}\n",
    ]

    # Render sections in spec order
    section_renderers = [
        ("environmental", "Environmental Screening", lambda d: _render_environmental(d)),
        ("zoning", "Zoning & Supply Threat", lambda d: _render_zoning(d)),
        ("projections", "Demand Projections", lambda d: _render_projections(d, target_year)),
        ("schools", "School Quality", lambda d: _render_schools(d)),
        ("transit", "Transit Access", lambda d: _render_transit(d)),
        ("migration", "Migration Flows", lambda d: _render_migration(d)),
        ("property_tax", "Property Tax", lambda d: _render_property_tax(d)),
        ("flood_risk", "Flood Risk", lambda d: _render_flood_risk(d)),
    ]

    for key, title, renderer in section_renderers:
        mod = modules.get(key, {})
        if mod.get("status") == "ok":
            lines.append(renderer(mod["data"]))
        elif mod.get("status") == "error":
            lines.append(_section_unavailable(title, mod["error"]))

    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Render site profile report")
    parser.add_argument(
        "--config",
        required=True,
        help="Path to property YAML config",
    )
    parser.add_argument(
        "--html",
        action="store_true",
        help="Also generate styled HTML output",
    )
    args = parser.parse_args()

    with open(args.config) as f:
        config = yaml.safe_load(f)

    metro_slug = config["notes"]["metro_slug"]
    json_path = Path(f"data/public/processed/enrichment/{metro_slug}/site_enrichment.json")

    if not json_path.exists():
        console.print(f"[red]Enrichment JSON not found: {json_path}[/red]")
        console.print("Run ingest_enrichment.py first.")
        return

    with open(json_path) as f:
        enrichment = json.load(f)

    md = render_report(enrichment)

    # Determine output path from config
    report_path = config["outputs"]["report_path"]
    base_dir = Path(report_path).parent
    out_md = base_dir / "site_profile.md"
    out_md.parent.mkdir(parents=True, exist_ok=True)
    out_md.write_text(md)
    console.print(f"[green]Wrote: {out_md}[/green]")

    if args.html:
        from etl.md_to_styled_html import md_to_html

        out_html = base_dir / "site_profile.html"
        out_html.write_text(
            md_to_html(out_md.read_text(encoding="utf-8"), base_dir=out_md.parent),
            encoding="utf-8",
        )
        console.print(f"[green]Wrote: {out_html}[/green]")


if __name__ == "__main__":
    main()
