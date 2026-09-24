"""Run-id generator. Format: '<ISO-week>_gainesville[__rN]'."""

from __future__ import annotations

import datetime as dt

import duckdb


def next_run_id(conn: duckdb.DuckDBPyConnection, today: dt.date | None = None) -> str:
    today = today or dt.date.today()
    iso_year, iso_week, _ = today.isocalendar()
    base = f"{iso_year}-W{iso_week:02d}_gainesville"
    rows = conn.execute(
        "SELECT run_id FROM run_log WHERE run_id LIKE ? OR run_id = ?",
        [f"{base}__r%", base],
    ).fetchall()
    existing = {r[0] for r in rows}
    if base not in existing:
        return base
    n = 2
    while f"{base}__r{n}" in existing:
        n += 1
    return f"{base}__r{n}"
