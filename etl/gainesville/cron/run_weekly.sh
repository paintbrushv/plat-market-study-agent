#!/usr/bin/env bash
# Weekly cron entrypoint for the Gainesville TX listings tracker.
#
# Schedule: Mondays 06:00 local. Cron entry:
#   0 6 * * MON <repo>/etl/gainesville/cron/run_weekly.sh
#
# What this does:
#   1. Runs the weekly cadence (Craigslist + Apartments.com + Zillow + property_direct)
#   2. Re-renders the current-month MF tracker and SFR index reports
#   3. Logs everything to /tmp/gainesville-tracker.log with date-stamped entries
#
# Does NOT handle:
#   - Monthly cadence (ZORI ingest) — needs a separate `--cadence monthly` cron on the 1st
#   - Quarterly cadence (Cooke CAD import) — needs `--cadence quarterly` on Jan/Apr/Jul/Oct 1st
#   - klement enrichments (year_built + unit_count) — manual or separate quarterly cron
#
# Failure handling: exits non-zero on partial/failed runs so cron mails the operator.

set -euo pipefail

REPO="$(cd "$(dirname "$0")/../../.." && pwd)"
UV="${UV:-uv}"
LOG=/tmp/gainesville-tracker.log
DB="$REPO/data/local/gainesville.duckdb"
LOCK="$REPO/data/local/.gainesville.lock"
RUNS_DIR="$REPO/reports/gainesville-tx/_ops/runs"

# Date stamp for log lines and the report's --month value
NOW=$(date +'%Y-%m-%d %H:%M:%S')
MONTH=$(date +'%Y-%m')

# Make logs rotatable — keep last ~3MB, copy off older content.
# This is a simple size-based rotation; for serious ops use logrotate.
if [ -f "$LOG" ] && [ $(stat -f%z "$LOG" 2>/dev/null || echo 0) -gt 3145728 ]; then
    mv "$LOG" "${LOG}.1"
    gzip -f "${LOG}.1" || true
fi

{
    echo
    echo "=== $NOW — gainesville weekly cadence ==="
    cd "$REPO"

    # 1. Weekly scrape cadence
    "$UV" run python -m etl.gainesville.run_weekly \
        --cadence weekly \
        --db "$DB" \
        --lock "$LOCK" \
        --runs-dir "$RUNS_DIR"

    # 2. Re-render current month's reports so they stay fresh
    "$UV" run python -m etl.gainesville.render_mf_tracker \
        --db "$DB" \
        --month "$MONTH" \
        --out "$REPO/reports/gainesville-tx/multifamily/_universe/monthly/${MONTH}-mf-tracker.md" \
        --html

    "$UV" run python -m etl.gainesville.render_sfr_index \
        --db "$DB" \
        --month "$MONTH" \
        --zip 76240 \
        --out "$REPO/reports/gainesville-tx/sfr/_index/monthly/${MONTH}-sfr-index.md" \
        --html

    echo "=== $NOW — done ==="
} >> "$LOG" 2>&1
