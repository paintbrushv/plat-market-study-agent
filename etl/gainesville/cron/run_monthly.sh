#!/usr/bin/env bash
# Monthly cron entrypoint for the Gainesville TX listings tracker.
#
# Schedule: 1st of each month at 07:00 local. Cron entry:
#   0 7 1 * * <repo>/etl/gainesville/cron/run_monthly.sh
#
# What this does:
#   1. Runs the monthly cadence (ZORI rent-index ingest)
#   2. Re-renders the new month's MF tracker and SFR index reports
#   3. Logs to /tmp/gainesville-tracker.log
#
# Why a separate schedule from weekly: ZORI publishes updates monthly, no
# point hitting Zillow's static CSV server more often than that.

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
    echo "=== $NOW — gainesville monthly cadence ==="
    cd "$REPO"

    # 1. Monthly cadence — currently just ZORI
    "$UV" run python -m etl.gainesville.run_weekly \
        --cadence monthly \
        --db "$DB" \
        --lock "$LOCK" \
        --runs-dir "$RUNS_DIR"

    # 2. Render the new month's reports (1st of month → fresh report file)
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
