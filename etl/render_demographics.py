"""Render demographics & labor shed markdown report from parquet outputs.

Reads the parquet files produced by ingest_demographics.py and generates
a markdown report section focused on the value-add investment thesis:

1. Who lives here NOW (10-min ring) — income, renter profile, housing stock
2. What jobs/labor exist in driving proximity (15/20-min rings)
3. Where workers commute from (labor shed)
4. Can the local workforce afford current AND post-renovation rents?
5. Demographic trajectory — is this area strengthening or weakening?

Usage:
    uv run python etl/render_demographics.py --config agents/configs/dallas_tx_republics.yaml
    uv run python etl/render_demographics.py --config agents/configs/dallas_tx_republics.yaml --html
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd
import yaml
from rich.console import Console

console = Console()


def _fmt_num(val, prefix="", suffix="", decimals=0):
    """Format a number with commas, optional prefix/suffix."""
    if pd.isna(val) or val is None:
        return "—"
    if decimals == 0:
        return f"{prefix}{int(val):,}{suffix}"
    return f"{prefix}{val:,.{decimals}f}{suffix}"


def _fmt_pct(val, decimals=1):
    if pd.isna(val) or val is None:
        return "—"
    return f"{val * 100:.{decimals}f}%"


def _fmt_dollar(val, decimals=0):
    return _fmt_num(val, prefix="$", decimals=decimals)


def _load_parquets(out_dir: Path) -> dict[str, pd.DataFrame]:
    """Load all parquet outputs into a dict."""
    data = {}
    for f in out_dir.glob("*.parquet"):
        try:
            data[f.stem] = pd.read_parquet(f)
        except Exception as e:
            console.print(f"[yellow]Could not load {f.name}: {e}[/yellow]")
    return data


def render_report(config: dict) -> str:
    """Render the demographics report section as markdown."""
    from geostack.fips import county_name_bulk

    subject = config["notes"]["subject_property"]
    demo_config = config["demographics"]
    metro_slug = config["notes"]["metro_slug"]

    property_name = subject["name"]
    address = subject["address"]
    # Use ``or 0`` — explicit YAML null returns None from .get() default.
    avg_rent = subject.get("avg_rent") or 0
    post_reno_rent = subject.get("post_reno_rent") or 0
    state_fips = demo_config["state_fips"]
    county_fips = demo_config["county_fips"]
    acs_year = demo_config["acs_year"]
    lodes_year = demo_config["lodes_year"]
    qcew_year = demo_config["qcew_year"]
    rings = demo_config["drive_time_minutes"]

    out_dir = Path(f"data/public/processed/demographics/{metro_slug}")
    data = _load_parquets(out_dir)

    if "demographics_summary" not in data:
        return "> Demographics data not available. Run `ingest_demographics.py` first.\n"

    summary = data["demographics_summary"]

    # --- Header ---
    lines = []
    lines.append(f"### Demographic & Labor Shed Analysis\n")
    lines.append(f"**Subject**: {property_name} — {address}  ")
    lines.append(f"**Method**: Drive-time isochrones (OpenRouteService)  ")
    lines.append(f"**Data**: ACS {acs_year} 5-Year, LODES {lodes_year}, BLS QCEW {qcew_year}\n")

    # --- Immediate Area Profile (smallest ring) ---
    smallest = summary.iloc[0]
    lines.append(f"#### Immediate Area ({int(smallest['minutes'])}-Minute Drive Time)\n")
    lines.append(
        f"The immediate catchment around {property_name} encompasses "
        f"**{_fmt_num(smallest['total_population'])} residents** across "
        f"**{int(smallest['tract_count'])} census tracts**. "
        f"Median household income is **{_fmt_dollar(smallest.get('median_household_income'))}**, "
        f"and **{_fmt_pct(smallest.get('renter_pct'))}** of occupied units are renter-occupied. "
        f"Market median rent is **{_fmt_dollar(smallest.get('median_rent'))}/mo** "
        f"with a **{_fmt_pct(smallest.get('vacancy_rate'))}** vacancy rate.\n"
    )

    # --- Population & Housing Table (all rings) ---
    lines.append("#### Population & Housing by Drive Time\n")
    lines.append("| Metric | " + " | ".join(f"{int(r['minutes'])}-min" for _, r in summary.iterrows()) + " | Source |")
    lines.append("| --- | " + " | ".join("---:" for _ in summary.iterrows()) + " | --- |")

    metrics = [
        ("Population", "total_population", _fmt_num),
        ("Median Age", "median_age", lambda v: _fmt_num(v, decimals=1)),
        ("Median HH Income", "median_household_income", _fmt_dollar),
        ("Per Capita Income", "per_capita_income", _fmt_dollar),
        ("Housing Units", "total_housing_units", _fmt_num),
        ("Vacancy Rate", "vacancy_rate", _fmt_pct),
        ("Renter %", "renter_pct", _fmt_pct),
        ("Median Market Rent", "median_rent", _fmt_dollar),
        ("Census Tracts", "tract_count", lambda v: _fmt_num(v)),
    ]

    for label, col, fmt in metrics:
        vals = " | ".join(fmt(r.get(col)) for _, r in summary.iterrows())
        source = f"[CENSUS-ACS, {acs_year}]"
        lines.append(f"| {label} | {vals} | {source} |")
    lines.append("")

    # --- Employment Profile ---
    primary_ring = rings[len(rings) // 2] if len(rings) > 1 else rings[0]
    emp_key = f"employment_summary_{primary_ring}min"

    if emp_key in data:
        emp = data[emp_key].iloc[0]
        total_jobs = emp.get("total_jobs", 0)

        lines.append(f"#### Employment Profile ({primary_ring}-Minute Catchment)\n")
        lines.append(
            f"There are **{_fmt_num(total_jobs)} jobs** within a {primary_ring}-minute drive. "
        )

        # Wage tier breakdown
        low = emp.get("jobs_earn_low", 0)
        mid = emp.get("jobs_earn_mid", 0)
        high = emp.get("jobs_earn_high", 0)
        if total_jobs > 0:
            lines.append(
                f"By LODES earnings tier: **{_fmt_pct(low/total_jobs)}** earn ≤$15K/yr, "
                f"**{_fmt_pct(mid/total_jobs)}** earn $15K–$40K/yr, "
                f"and **{_fmt_pct(high/total_jobs)}** earn >$40K/yr.\n"
            )

        lines.append("| Earnings Tier | Jobs | Share | Implied Annual Range | Source |")
        lines.append("| --- | ---: | ---: | --- | --- |")
        lines.append(f"| Low (≤$1,250/mo) | {_fmt_num(low)} | {_fmt_pct(low/total_jobs) if total_jobs else '—'} | ≤$15,000 | [LODES, {lodes_year}] |")
        lines.append(f"| Mid ($1,251–$3,333/mo) | {_fmt_num(mid)} | {_fmt_pct(mid/total_jobs) if total_jobs else '—'} | $15,001–$40,000 | [LODES, {lodes_year}] |")
        lines.append(f"| High (>$3,333/mo) | {_fmt_num(high)} | {_fmt_pct(high/total_jobs) if total_jobs else '—'} | >$40,000 | [LODES, {lodes_year}] |")
        lines.append(f"| **Total** | **{_fmt_num(total_jobs)}** | **100%** | | |")
        lines.append("")

    # --- QCEW Top Sectors ---
    if "qcew_top_sectors" in data:
        qs = data["qcew_top_sectors"]
        lines.append(f"#### Top Employment Sectors — {county_name_bulk([(state_fips, county_fips)]).get(f'{state_fips}{county_fips}', 'County')}\n")
        lines.append("| Sector | Jobs | Avg Annual Pay | Source |")
        lines.append("| --- | ---: | ---: | --- |")
        for _, r in qs.iterrows():
            title = str(r.get("naics_title", r.get("industry_title", "Unknown")))
            # Clean up NAICS titles — remove "NAICS XX" prefix if present
            if " - " in title:
                title = title.split(" - ", 1)[1]
            title = title[:50]
            emp_val = r.get("annual_avg_employment", r.get("annual_avg_emplvl", 0))
            pay_val = r.get("avg_annual_pay", 0)
            lines.append(
                f"| {title} | {_fmt_num(emp_val)} | {_fmt_dollar(pay_val)} | [QCEW, {qcew_year}] |"
            )
        lines.append("")

    # --- Commute Shed / Labor Shed ---
    if "commute_shed" in data:
        shed = data["commute_shed"]
        lines.append(f"#### Labor Shed — Where the Workforce Lives\n")

        # Resolve county names
        fips_pairs = [
            (r["h_state_fips"], r["h_county_fips"])
            for _, r in shed.head(15).iterrows()
        ]
        names = county_name_bulk(fips_pairs)

        total_workers = shed["total_jobs"].sum()
        lines.append(
            f"LODES origin-destination data shows **{_fmt_num(total_workers)} workers** "
            f"commuting into the {primary_ring}-minute catchment from "
            f"**{len(shed)} counties** across {shed['h_state_fips'].nunique()} states.\n"
        )

        lines.append("| Home County | State | Workers | Share | Cumulative | Source |")
        lines.append("| --- | --- | ---: | ---: | ---: | --- |")
        for _, r in shed.head(12).iterrows():
            key = f"{r['h_state_fips']}{r['h_county_fips']}"
            cname = names.get(key, key)
            from geostack.fips import state_name
            sname = state_name(r["h_state_fips"])
            lines.append(
                f"| {cname} | {sname} | {_fmt_num(r['total_jobs'])} | "
                f"{_fmt_pct(r['pct_of_total'])} | {_fmt_pct(r['cumulative_pct'])} | "
                f"[LODES OD, {lodes_year}] |"
            )

        # Summary insight
        top5 = shed.head(5)
        top5_pct = top5["pct_of_total"].sum()
        top5_names = []
        for _, r in top5.iterrows():
            key = f"{r['h_state_fips']}{r['h_county_fips']}"
            top5_names.append(names.get(key, key))

        lines.append("")
        lines.append(
            f"> The top 5 counties ({', '.join(top5_names)}) account for "
            f"**{_fmt_pct(top5_pct)}** of the workforce. "
        )

        # Home county share (workers who live AND work in the same county)
        home_county_key = f"{state_fips}{county_fips}"
        home_row = shed[
            (shed["h_state_fips"] == state_fips)
            & (shed["h_county_fips"] == county_fips)
        ]
        if not home_row.empty:
            home_pct = home_row.iloc[0]["pct_of_total"]
            lines.append(
                f"**{_fmt_pct(home_pct)}** of workers live within "
                f"{names.get(home_county_key, 'the home county')} itself."
            )
        lines.append("")

    # --- Workforce-Housing Affordability ---
    lines.append("#### Workforce-Housing Affordability\n")

    # Use the middle ring for affordability context
    mid_idx = len(summary) // 2
    mid_row = summary.iloc[mid_idx]
    median_income = mid_row.get("median_household_income", 0) or 0
    ring_label = f"{int(mid_row['minutes'])}-minute catchment"

    # Current rent
    current_required = avg_rent * 12 / 0.30 if avg_rent > 0 else 0
    current_ratio = median_income / current_required if current_required > 0 else 0

    lines.append("| Scenario | Monthly Rent | Required Income (30%) | Catchment Median Income | Ratio | Verdict |")
    lines.append("| --- | ---: | ---: | ---: | ---: | --- |")

    verdict_current = "Affordable" if current_ratio >= 1.0 else "Rent-Burdened"
    lines.append(
        f"| **Current** | {_fmt_dollar(avg_rent)}/mo | "
        f"{_fmt_dollar(current_required)}/yr | {_fmt_dollar(median_income)}/yr | "
        f"{current_ratio:.2f}x | **{verdict_current}** |"
    )

    # Post-reno rent
    if post_reno_rent and post_reno_rent > 0:
        reno_required = post_reno_rent * 12 / 0.30
        reno_ratio = median_income / reno_required if reno_required > 0 else 0
        verdict_reno = "Affordable" if reno_ratio >= 1.0 else "Rent-Burdened"
        lines.append(
            f"| **Post-Renovation** | {_fmt_dollar(post_reno_rent)}/mo | "
            f"{_fmt_dollar(reno_required)}/yr | {_fmt_dollar(median_income)}/yr | "
            f"{reno_ratio:.2f}x | **{verdict_reno}** |"
        )
    lines.append("")

    # Affordability narrative
    lines.append(f"*Based on {ring_label} median household income of {_fmt_dollar(median_income)}/yr.*\n")

    if post_reno_rent and post_reno_rent > 0:
        rent_bump = post_reno_rent - avg_rent
        rent_bump_pct = rent_bump / avg_rent if avg_rent > 0 else 0
        reno_required = post_reno_rent * 12 / 0.30
        reno_ratio = median_income / reno_required if reno_required > 0 else 0

        if reno_ratio >= 1.3:
            assessment = (
                f"The post-renovation target of {_fmt_dollar(post_reno_rent)}/mo "
                f"(+{_fmt_dollar(rent_bump)}, +{_fmt_pct(rent_bump_pct, 0)}) is **well-supported** "
                f"by local incomes at {reno_ratio:.2f}x the 30% threshold. "
                f"Significant headroom exists for the value-add strategy."
            )
        elif reno_ratio >= 1.0:
            assessment = (
                f"The post-renovation target of {_fmt_dollar(post_reno_rent)}/mo "
                f"(+{_fmt_dollar(rent_bump)}, +{_fmt_pct(rent_bump_pct, 0)}) is **achievable** "
                f"at {reno_ratio:.2f}x the 30% threshold, though headroom is modest. "
                f"Lease-up velocity at the higher rent may be slower."
            )
        else:
            assessment = (
                f"The post-renovation target of {_fmt_dollar(post_reno_rent)}/mo "
                f"(+{_fmt_dollar(rent_bump)}, +{_fmt_pct(rent_bump_pct, 0)}) **exceeds** "
                f"what the median household can comfortably afford ({reno_ratio:.2f}x). "
                f"The value-add strategy depends on attracting higher-income tenants "
                f"or targeting the upper income quartile in the catchment."
            )
        lines.append(f"> {assessment}\n")

    # --- Income Distribution & Precise Affordability ---
    if "income_distribution" in data:
        from geostack.demographics import affordability_at_rent

        income_df = data["income_distribution"]
        if not income_df.empty:
            lines.append("#### Income Distribution & Affordability Depth\n")

            # Calculate precise affordability at current and post-reno
            pct_afford_current = affordability_at_rent(income_df, avg_rent) if avg_rent else 0
            pct_afford_reno = affordability_at_rent(income_df, post_reno_rent) if post_reno_rent else 0

            lines.append(
                f"Based on the full household income distribution within the "
                f"{primary_ring}-minute catchment:\n"
            )

            lines.append("| Rent Scenario | Monthly Rent | HH% That Can Afford | Assessment |")
            lines.append("| --- | ---: | ---: | --- |")

            def _assess(pct):
                if pct >= 0.60:
                    return "Strong depth"
                elif pct >= 0.45:
                    return "Adequate depth"
                elif pct >= 0.30:
                    return "Thin — lease-up risk"
                else:
                    return "Warning — limited pool"

            if avg_rent:
                lines.append(
                    f"| Current | {_fmt_dollar(avg_rent)}/mo | "
                    f"{_fmt_pct(pct_afford_current)} | {_assess(pct_afford_current)} |"
                )
            if post_reno_rent:
                lines.append(
                    f"| Post-Renovation | {_fmt_dollar(post_reno_rent)}/mo | "
                    f"{_fmt_pct(pct_afford_reno)} | {_assess(pct_afford_reno)} |"
                )
            lines.append("")

            # Income bracket table (condensed)
            lines.append("| Income Bracket | Households | Share | Cumulative |")
            lines.append("| --- | ---: | ---: | ---: |")
            for _, r in income_df.iterrows():
                lines.append(
                    f"| {r['bracket']} | {_fmt_num(r['households'])} | "
                    f"{_fmt_pct(r['pct'])} | {_fmt_pct(r['cumulative_pct'])} |"
                )
            lines.append("")

    # --- Rent Position ---
    if "rent_distribution" in data:
        rent_dist = data["rent_distribution"].iloc[0] if not data["rent_distribution"].empty else {}
        rent_total = rent_dist.get("rent_total", 0) or 0
        if rent_total > 0:
            lines.append("#### Rent Position in Local Market\n")
            brackets = [
                ("Under $800", rent_dist.get("rent_under_800", 0)),
                ("$800–$999", rent_dist.get("rent_800_899", 0) + rent_dist.get("rent_900_999", 0)),
                ("$1,000–$1,499", rent_dist.get("rent_1000_1249", 0) + rent_dist.get("rent_1250_1499", 0)),
                ("$1,500–$1,999", rent_dist.get("rent_1500_1999", 0)),
                ("$2,000–$2,499", rent_dist.get("rent_2000_2499", 0)),
                ("$2,500–$2,999", rent_dist.get("rent_2500_2999", 0)),
                ("$3,000+", rent_dist.get("rent_3000_3499", 0) + rent_dist.get("rent_3500_plus", 0)),
            ]
            lines.append("| Rent Bracket | Renter HHs | Share | Source |")
            lines.append("| --- | ---: | ---: | --- |")
            for label, count in brackets:
                pct = count / rent_total if rent_total else 0
                # Mark the bracket where the subject property's rent falls
                marker = ""
                if avg_rent:
                    rent_bracket_ranges = [
                        ("Under $800", 0, 800),
                        ("$800", 800, 1000),
                        ("$1,000", 1000, 1500),
                        ("$1,500", 1500, 2000),
                        ("$2,000", 2000, 2500),
                        ("$2,500", 2500, 3000),
                        ("$3,000", 3000, 999999),
                    ]
                    for prefix, lo, hi in rent_bracket_ranges:
                        if label.startswith(prefix) and lo <= avg_rent < hi:
                            marker = " **← subject**"
                            break
                lines.append(
                    f"| {label}{marker} | {_fmt_num(count)} | {_fmt_pct(pct)} | [CENSUS-ACS, {acs_year}] |"
                )
            lines.append("")

    # --- Education Profile ---
    if "education" in data:
        edu = data["education"].iloc[0] if not data["education"].empty else {}
        edu_total = edu.get("edu_total_25plus", 0) or 0
        if edu_total > 0:
            bachelors_plus = (
                edu.get("edu_bachelors", 0) + edu.get("edu_masters", 0)
                + edu.get("edu_professional", 0) + edu.get("edu_doctorate", 0)
            )
            ba_pct = bachelors_plus / edu_total

            lines.append("#### Education Attainment (25+ Population)\n")
            lines.append(
                f"**{_fmt_pct(ba_pct)}** of the 25+ population holds a bachelor's degree or higher "
                f"({_fmt_num(bachelors_plus)} of {_fmt_num(edu_total)}).\n"
            )

            edu_rows = [
                ("High School Diploma", edu.get("edu_hs_diploma", 0)),
                ("Some College", edu.get("edu_some_college", 0)),
                ("Associate's", edu.get("edu_associates", 0)),
                ("Bachelor's", edu.get("edu_bachelors", 0)),
                ("Master's", edu.get("edu_masters", 0)),
                ("Professional/Doctorate", edu.get("edu_professional", 0) + edu.get("edu_doctorate", 0)),
            ]
            lines.append("| Attainment | Count | Share |")
            lines.append("| --- | ---: | ---: |")
            for label, count in edu_rows:
                lines.append(f"| {label} | {_fmt_num(count)} | {_fmt_pct(count / edu_total)} |")
            lines.append("")

    # --- Go / No-Go Assessment ---
    lines.append("#### Investment Assessment\n")
    signals = []
    warnings = []

    # Affordability check
    if "income_distribution" in data and not data["income_distribution"].empty:
        from geostack.demographics import affordability_at_rent
        income_df = data["income_distribution"]
        if post_reno_rent:
            pct = affordability_at_rent(income_df, post_reno_rent)
            if pct >= 0.45:
                signals.append(f"Post-reno rent affordable to {_fmt_pct(pct)} of households")
            elif pct >= 0.30:
                warnings.append(f"Post-reno rent affordable to only {_fmt_pct(pct)} of households — thin demand pool")
            else:
                warnings.append(f"Post-reno rent affordable to only {_fmt_pct(pct)} of households — significant lease-up risk")

    # Income trend
    if "acs_cagr" in data and not data["acs_cagr"].empty:
        income_cagr_row = data["acs_cagr"][data["acs_cagr"]["variable"] == "median_household_income"]
        if not income_cagr_row.empty:
            ic = income_cagr_row.iloc[0]["cagr"]
            if ic > 0.03:
                signals.append(f"Strong income growth ({_fmt_pct(ic)} CAGR)")
            elif ic > 0.01:
                signals.append(f"Moderate income growth ({_fmt_pct(ic)} CAGR)")
            else:
                warnings.append(f"Weak income growth ({_fmt_pct(ic)} CAGR)")

    # Renter base
    mid_row = summary.iloc[len(summary) // 2]
    renter_pct = mid_row.get("renter_pct", 0) or 0
    if renter_pct > 0.45:
        signals.append(f"Strong renter base ({_fmt_pct(renter_pct)} renter-occupied)")
    elif renter_pct > 0.30:
        signals.append(f"Moderate renter base ({_fmt_pct(renter_pct)})")
    else:
        warnings.append(f"Low renter share ({_fmt_pct(renter_pct)}) — owner-dominated market")

    # Vacancy
    vacancy = mid_row.get("vacancy_rate", 0) or 0
    if vacancy > 0.10:
        warnings.append(f"Elevated vacancy ({_fmt_pct(vacancy)})")
    elif vacancy < 0.05:
        signals.append(f"Tight market ({_fmt_pct(vacancy)} vacancy)")

    if signals:
        lines.append("**Positive signals:**")
        for s in signals:
            lines.append(f"- {s}")
        lines.append("")

    if warnings:
        lines.append("**Warnings:**")
        for w in warnings:
            lines.append(f"- {w}")
        lines.append("")

    # Overall verdict
    if len(warnings) == 0 and len(signals) >= 2:
        lines.append("> **GO** — Demographics strongly support the value-add strategy.\n")
    elif len(warnings) <= 1 and len(signals) >= 1:
        lines.append("> **CONDITIONAL GO** — Demographics are supportive with noted caveats.\n")
    else:
        lines.append("> **CAUTION** — Review warnings before proceeding with value-add execution.\n")

    # --- Demographic Trajectory ---
    if "acs_trends" in data and "acs_cagr" in data:
        trends = data["acs_trends"]
        cagr = data["acs_cagr"]

        years = sorted(trends["year"].unique())
        if len(years) >= 2:
            lines.append(f"#### Demographic Trajectory ({int(years[0])}–{int(years[-1])})\n")

            lines.append(f"| Metric | {int(years[0])} | {int(years[-1])} | CAGR | Direction | Source |")
            lines.append("| --- | ---: | ---: | ---: | --- | --- |")

            track_vars = [
                ("total_population", "Population", _fmt_num),
                ("median_household_income", "Median HH Income", _fmt_dollar),
                ("renter_occupied", "Renter-Occupied Units", _fmt_num),
                ("median_rent", "Median Rent", _fmt_dollar),
                ("total_housing_units", "Housing Units", _fmt_num),
            ]

            for var_name, label, fmt in track_vars:
                var_data = trends[trends["variable"] == var_name].sort_values("year")
                cagr_row = cagr[cagr["variable"] == var_name] if not cagr.empty else pd.DataFrame()

                if len(var_data) >= 2:
                    first_val = var_data.iloc[0]["value"]
                    last_val = var_data.iloc[-1]["value"]
                    cagr_val = cagr_row.iloc[0]["cagr"] if not cagr_row.empty else None

                    if cagr_val is not None:
                        direction = "Rising" if cagr_val > 0.005 else ("Flat" if cagr_val > -0.005 else "Declining")
                    else:
                        direction = "—"

                    lines.append(
                        f"| {label} | {fmt(first_val)} | {fmt(last_val)} | "
                        f"{_fmt_pct(cagr_val, 1) if cagr_val is not None else '—'} | "
                        f"{direction} | [CENSUS-ACS] |"
                    )
            lines.append("")

            # Trajectory narrative
            pop_cagr = cagr[cagr["variable"] == "total_population"]
            income_cagr = cagr[cagr["variable"] == "median_household_income"]
            rent_cagr = cagr[cagr["variable"] == "median_rent"]

            signals = []
            if not pop_cagr.empty and pop_cagr.iloc[0]["cagr"] > 0.005:
                signals.append("growing population")
            if not income_cagr.empty and income_cagr.iloc[0]["cagr"] > 0.01:
                signals.append("rising incomes")
            if not rent_cagr.empty and rent_cagr.iloc[0]["cagr"] > 0.01:
                signals.append("rising rents")

            if len(signals) >= 2:
                lines.append(
                    f"> Positive trajectory: {', '.join(signals)} support "
                    f"the value-add thesis with organic demand growth.\n"
                )
            elif signals:
                lines.append(
                    f"> Mixed signal: {signals[0]}, but other indicators are flat. "
                    f"Value-add returns will depend more on execution than macro tailwinds.\n"
                )

    # --- Data Sources ---
    lines.append("---\n")
    lines.append("*Sources: U.S. Census Bureau ACS 5-Year Estimates, LEHD LODES Origin-Destination Employment Statistics, BLS Quarterly Census of Employment and Wages. ")
    lines.append("Drive-time catchments computed via OpenRouteService isochrone API. ")
    lines.append("Affordability assessed at the 30% rent-to-income standard.*\n")

    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Render demographics report section")
    parser.add_argument(
        "--config", required=True,
        help="Path to property YAML config",
    )
    parser.add_argument(
        "--html", action="store_true",
        help="Also convert to styled HTML via md_to_styled_html",
    )
    args = parser.parse_args()

    with open(args.config) as f:
        config = yaml.safe_load(f)

    report_md = render_report(config)

    # Write markdown
    metro_slug = config["notes"]["metro_slug"]
    # Derive report path from config
    slug_parts = metro_slug.replace("_", "-").rsplit("-", 1)
    if len(slug_parts) >= 2:
        metro_dir = "-".join(slug_parts[:-1])
        prop_dir = slug_parts[-1]
    else:
        metro_dir = slug_parts[0]
        prop_dir = slug_parts[0]

    # Use the outputs report path pattern
    out_base = config.get("outputs", {}).get("report_path", "")
    if out_base:
        report_dir = Path(out_base).parent
    else:
        report_dir = Path(f"reports/{metro_dir}/{prop_dir}")

    report_dir.mkdir(parents=True, exist_ok=True)
    md_path = report_dir / "demographics_labor_shed.md"
    md_path.write_text(report_md)
    console.print(f"[green]Report written: {md_path}[/green]")

    # Optionally convert to HTML
    if args.html:
        try:
            from etl.md_to_styled_html import convert_md_to_html

            html_path = md_path.with_suffix(".html")
            convert_md_to_html(str(md_path), str(html_path))
            console.print(f"[green]HTML written: {html_path}[/green]")
        except ImportError:
            console.print("[yellow]md_to_styled_html not available, skipping HTML conversion[/yellow]")

    # Generate visualization assets (maps + charts)
    try:
        import sys
        from pathlib import Path as _Path
        _project_root = str(_Path(__file__).resolve().parent.parent)
        if _project_root not in sys.path:
            sys.path.insert(0, _project_root)
        from etl.render_demographics_viz import generate_demographic_viz

        viz_paths = generate_demographic_viz(config, report_dir)
        if viz_paths:
            console.print(f"[green]Visualization assets ({len(viz_paths)} files):[/green]")
            for p in viz_paths:
                console.print(f"  {p}")
    except ImportError as e:
        console.print(f"[dim]Skipping viz ({e})[/dim]")
    except Exception as e:
        console.print(f"[yellow]Viz generation failed (non-blocking): {e}[/yellow]")

    # Print to stdout as well
    console.print("\n" + report_md)


if __name__ == "__main__":
    main()
