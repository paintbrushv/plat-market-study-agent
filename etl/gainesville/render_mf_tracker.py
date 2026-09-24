"""Monthly MF tracker renderer.

Produces a markdown report from DuckDB queries. --for-edc redacts any
specific addresses or per-listing detail (only aggregates and
property-register-level breakdowns).
"""

from __future__ import annotations

import argparse
import calendar
import datetime as dt
import sys
from io import StringIO
from pathlib import Path

import duckdb

from etl.gainesville.db import connect
from etl.gainesville.queries import (
    asking_rent_quartiles_by_beds,
    coverage_diagnostics,
    listings_unmatched_to_register,
    owner_concentration_top_n,
    universe_summary,
)


def render(
    conn: duckdb.DuckDBPyConnection,
    *,
    month: dt.date,
    for_edc: bool,
) -> str:
    out = StringIO()
    out.write("# Gainesville, TX (Cooke County) — Multifamily Tracker\n\n")
    out.write(f"**Month:** {month.strftime('%B %Y')}  \n")
    out.write(f"**Generated:** {dt.date.today().isoformat()}  \n")
    if for_edc:
        out.write("**Audience:** EDC board (redacted)  \n\n")
    else:
        out.write("**Audience:** Internal — personal investor radar  \n\n")

    # Universe
    summary = universe_summary(conn)
    out.write("## Universe\n\n")
    out.write(f"- **Total MF properties:** {summary['properties_total']}\n")
    out.write(f"- **Total MF units:** {summary['units_total']:,}\n")
    out.write(
        f"- **In Gainesville city limits:** {summary['properties_in_city']} properties"
        f" / {summary['units_in_city']:,} units\n"
    )
    out.write(
        f"- **Out of city / county-only:** "
        f"{summary['properties_total'] - summary['properties_in_city']} properties\n\n"
    )

    # Owner concentration
    top = owner_concentration_top_n(conn, n=10)
    if top:
        out.write("### Owner concentration (top 10)\n\n")
        out.write("| Owner | Properties | Units |\n|---|---:|---:|\n")
        for row in top:
            out.write(f"| {row['owner']} | {row['properties']} | {row['units']:,} |\n")
        out.write("\n")

    # Active listings
    out.write("## Active listings\n\n")
    n_active = int(
        conn.execute(
            "SELECT COUNT(*) FROM canonical_listings WHERE status = 'active' "
            "AND listing_kind IN ('mf', 'duplex', 'fourplex')"
        ).fetchone()[0]
    )
    out.write(f"**Currently advertised MF listings:** {n_active}\n\n")
    if not for_edc:
        # Per-property breakdown — omitted in EDC mode to avoid exposing specific addresses.
        rows = conn.execute(
            "SELECT pr.name, pr.address, COUNT(cl.canonical_id) AS n_listings, "
            "       MIN(cl.current_rent) AS min_rent, MAX(cl.current_rent) AS max_rent "
            "FROM canonical_listings cl LEFT JOIN property_register pr "
            "ON cl.property_id = pr.property_id "
            "WHERE cl.status = 'active' "
            "GROUP BY pr.name, pr.address ORDER BY n_listings DESC, pr.name "
            "LIMIT 20"
        ).fetchall()
        if rows:
            out.write(
                "| Property | Address | Listings | Min rent | Max rent |\n"
                "|---|---|---:|---:|---:|\n"
            )
            for r in rows:
                name = r[0] or "_(unmatched)_"
                addr = r[1] or "—"
                min_rent = f"${r[3]:,}" if r[3] else "—"
                max_rent = f"${r[4]:,}" if r[4] else "—"
                out.write(f"| {name} | {addr} | {r[2]} | {min_rent} | {max_rent} |\n")
            out.write("\n")

    # Asking rents
    out.write("## Asking rents\n\n")
    quartiles = asking_rent_quartiles_by_beds(conn)
    if quartiles:
        out.write(
            "| Beds | Count | p25 | Median | p75 | Median $/SF |\n"
            "|---:|---:|---:|---:|---:|---:|\n"
        )
        for beds in sorted(quartiles.keys()):
            q = quartiles[beds]
            if q["median"] is not None:
                psf_cell = f"${q['median_psf']:.2f}" if q["median_psf"] is not None else "—"
                out.write(
                    f"| {int(beds)} | {q['count']} | "
                    f"${q['p25']:,} | ${q['median']:,} | ${q['p75']:,} | "
                    f"{psf_cell} |\n"
                )
            else:
                out.write(f"| {int(beds)} | {q['count']} | — | — | — | — |\n")
        out.write("\n")
    else:
        out.write("_No active listings with rent data this month._\n\n")

    # Coverage diagnostics
    month_start = month.replace(day=1).isoformat()
    month_end = (
        month.replace(day=calendar.monthrange(month.year, month.month)[1])
        + dt.timedelta(days=1)
    ).isoformat()
    cov = coverage_diagnostics(conn, month_start=month_start, month_end=month_end)
    out.write("## Coverage diagnostics\n\n")
    out.write("**Observations per source:**\n")
    for src, n in sorted(cov["obs_per_source"].items()):
        out.write(f"- {src}: {n:,}\n")
    if cov["failed_runs"]:
        out.write(f"\n**Failed runs this month:** {len(cov['failed_runs'])}\n")
        for fr in cov["failed_runs"]:
            out.write(f"- {fr['run_id']}: failed sources = {fr['failed']}\n")
    out.write(f"\n**Open dedup review queue:** {cov['review_queue_open']}\n")
    n_unmatched = listings_unmatched_to_register(conn)
    if n_unmatched:
        out.write(
            f"\n**MF listings unmatched to property register:** {n_unmatched} "
            "(possible new builds or address typos)\n"
        )
    return out.getvalue()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=Path("data/local/gainesville.duckdb"))
    parser.add_argument("--month", type=str, help="YYYY-MM (defaults to current month)")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--for-edc", action="store_true")
    parser.add_argument(
        "--html",
        action="store_true",
        help="Also write a styled HTML version next to the markdown.",
    )
    args = parser.parse_args(argv)
    month = (
        dt.date.fromisoformat(args.month + "-01")
        if args.month
        else dt.date.today().replace(day=1)
    )
    conn = connect(args.db)
    md = render(conn, month=month, for_edc=args.for_edc)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(md, encoding="utf-8")
    print(f"wrote {args.out}")
    if args.html:
        from etl.md_to_styled_html import md_to_html

        html_out = args.out.with_suffix(".html")
        html_content = md_to_html(md, base_dir=args.out.parent)
        html_out.write_text(html_content, encoding="utf-8")
        print(f"wrote {html_out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
