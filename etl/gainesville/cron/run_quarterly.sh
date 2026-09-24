#!/usr/bin/env bash
# Quarterly cron entrypoint for the Gainesville TX listings tracker.
#
# Schedule: 1st of Jan/Apr/Jul/Oct at 08:00 local. Cron entry:
#   0 8 1 1,4,7,10 * <repo>/etl/gainesville/cron/run_quarterly.sh
#
# What this does:
#   1. Runs the quarterly cadence (cooke_cad_import from klement.db)
#   2. Re-renders the MF tracker (property register may have changed)
#   3. Logs to /tmp/gainesville-tracker.log
#
# What this DOES NOT do — klement's enrichment scripts:
#   - `enrich_unit_count.py` and `enrich_mf_year_built.py` use Playwright in
#     headed mode (Akamai WAF blocks headless), which requires an active
#     macOS window-server session. Cron jobs on macOS don't reliably get
#     that. So those stay manual.
#
#   If you've added new MF properties to klement.db (rare — quarterly at
#   most), run these BEFORE this cron fires:
#     cd ~/projects/klement
#     venv/bin/python -m etl.scripts.enrich_unit_count --force
#     venv/bin/python -m etl.scripts.enrich_mf_year_built --force
#
#   The market-study quarterly cadence below picks up whatever's currently
#   in klement.db — including any operator overrides you've added since
#   the last run.

set -euo pipefail

REPO="$(cd "$(dirname "$0")/../../.." && pwd)"
UV="${UV:-uv}"
LOG=/tmp/gainesville-tracker.log
DB="$REPO/data/local/gainesville.duckdb"
LOCK="$REPO/data/local/.gainesville.lock"
RUNS_DIR="$REPO/reports/gainesville-tx/_ops/runs"

NOW=$(date +'%Y-%m-%d %H:%M:%S')
MONTH=$(date +'%Y-%m')

# Size-based log rotation (3MB cap, keep one compressed historical)
if [ -f "$LOG" ] && [ $(stat -f%z "$LOG" 2>/dev/null || echo 0) -gt 3145728 ]; then
    mv "$LOG" "${LOG}.1"
    gzip -f "${LOG}.1" || true
fi

{
    echo
    echo "=== $NOW — gainesville quarterly cadence ==="
    cd "$REPO"

    # 1. Quarterly cadence — refreshes property_register from klement.db
    "$UV" run python -m etl.gainesville.run_weekly \
        --cadence quarterly \
        --db "$DB" \
        --lock "$LOCK" \
        --runs-dir "$RUNS_DIR"

    # 2. Re-render the MF tracker (property universe may have changed)
    "$UV" run python -m etl.gainesville.render_mf_tracker \
        --db "$DB" \
        --month "$MONTH" \
        --out "$REPO/reports/gainesville-tx/multifamily/_universe/monthly/${MONTH}-mf-tracker.md" \
        --html

    # SFR index isn't affected by property_register changes but render anyway
    # so reports stay synchronized
    "$UV" run python -m etl.gainesville.render_sfr_index \
        --db "$DB" \
        --month "$MONTH" \
        --zip 76240 \
        --out "$REPO/reports/gainesville-tx/sfr/_index/monthly/${MONTH}-sfr-index.md" \
        --html

    echo "=== $NOW — done ==="
} >> "$LOG" 2>&1
