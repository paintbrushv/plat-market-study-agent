"""Monthly SFR rent-index renderer.

Compares Zillow's published ZORI to our scraped median asking rent.
Both are imperfect — ZORI is smoothed and ZIP-level; ours has thin
sample size in a town this small. We surface both, plus the divergence,
plus enough coverage detail that the reader can judge sample quality.
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
from etl.gainesville.queries import coverage_diagnostics, sfr_local_median, zori_latest


def render(
    conn: duckdb.DuckDBPyConnection,
    *,
    month: dt.date,
    zip_code: str,
    for_edc: bool,
) -> str:
    out = StringIO()
    out.write("# Gainesville, TX — SFR Rent Index\n\n")
    out.write(f"**Month:** {month.strftime('%B %Y')}  \n")
    out.write(f"**Generated:** {dt.date.today().isoformat()}  \n")
    out.write(f"**ZIP filter:** {zip_code}  \n\n")

    # ZORI benchmark
    out.write("## ZORI benchmark\n\n")
    z = zori_latest(conn, zip_code, "all")
    if z:
        out.write(f"- **Latest ZORI ({z['month']}):** ${z['value']:,.0f}/mo\n")
        if z["yoy_pct"] is not None:
            out.write(f"- **YoY change:** {z['yoy_pct']:+.1f}%\n")
        history = conn.execute(
            "SELECT month, zori_value FROM zori_history "
            "WHERE zip = ? AND home_type = 'all' AND month >= ? "
            "ORDER BY month",
            [zip_code, (month.replace(day=1) - dt.timedelta(days=365)).isoformat()],
        ).fetchall()
        if history:
            out.write("\n| Month | ZORI |\n|---|---:|\n")
            for m, v in history:
                out.write(f"| {m.isoformat()} | ${v:,.0f} |\n")
    else:
        out.write("_No ZORI data for this ZIP yet — run `--source zori` to fetch._\n")
    out.write("\n")

    # Local median asking rent
    out.write("## Local median asking rent\n\n")
    local = sfr_local_median(conn)
    if local["count"] > 0:
        out.write(f"- **SFR active listings sampled:** {local['count']}\n")
        out.write(f"- **Median asking rent:** ${local['median_rent']:,}/mo\n")
        if local["median_psf"] is not None:
            out.write(f"- **Median $/SF:** ${local['median_psf']:.2f}\n")
    else:
        out.write("_No SFR listings yet this month._\n")
    out.write("\n")

    # Divergence
    out.write("## ZORI vs. local divergence\n\n")
    if z and local["count"] > 0 and local["median_rent"] is not None:
        diff = local["median_rent"] - z["value"]
        pct = (diff / z["value"]) * 100
        out.write(f"- **Local − ZORI:** ${diff:+,.0f}/mo ({pct:+.1f}%)\n")
        if local["count"] < 10:
            out.write("- **Caveat:** local sample size below 10. Treat as directional only.\n")
    else:
        out.write("_Insufficient data to compute divergence._\n")
    out.write("\n")

    # Listing churn
    out.write("## Listing churn\n\n")
    new_this_month = int(
        conn.execute(
            "SELECT COUNT(*) FROM canonical_listings "
            "WHERE listing_kind = 'sfr' AND first_seen >= ?",
            [month.replace(day=1)],
        ).fetchone()[0]
    )
    rented_or_pulled = int(
        conn.execute(
            "SELECT COUNT(*) FROM canonical_listings "
            "WHERE listing_kind = 'sfr' AND status = 'rented_or_pulled' AND last_seen >= ?",
            [month.replace(day=1)],
        ).fetchone()[0]
    )
    out.write(f"- **New listings this month:** {new_this_month}\n")
    out.write(f"- **Listings rented or pulled:** {rented_or_pulled}\n\n")

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
    if not cov["obs_per_source"]:
        out.write("_No observations recorded this month._\n")
    if cov["failed_runs"]:
        out.write(f"\n**Failed runs this month:** {len(cov['failed_runs'])}\n")
        for fr in cov["failed_runs"]:
            out.write(f"- {fr['run_id']}: failed sources = {fr['failed']}\n")
    out.write(f"\n**Open dedup review queue:** {cov['review_queue_open']}\n")
    return out.getvalue()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Render monthly SFR rent-index report.")
    parser.add_argument("--db", type=Path, default=Path("data/local/gainesville.duckdb"))
    parser.add_argument("--month", type=str, help="YYYY-MM (defaults to current month)")
    parser.add_argument("--zip", type=str, default="76240")
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
    md = render(conn, month=month, zip_code=args.zip, for_edc=args.for_edc)
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
