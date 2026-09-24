from __future__ import annotations

import time

import pytest
import etl.collect_comps_snapshot as comps_snapshot

from etl.collect_comps_snapshot import (
    _CompScrapeDeadlineExceeded,
    load_subject_floorplan_summary,
    _run_with_deadline,
)


def test_run_with_deadline_returns_function_value() -> None:
    assert _run_with_deadline(1, "fast task", lambda: {"ok": True}) == {"ok": True}


def test_run_with_deadline_raises_on_hung_task() -> None:
    with pytest.raises(_CompScrapeDeadlineExceeded, match="hung task exceeded 1s"):
        _run_with_deadline(1, "hung task", lambda: time.sleep(2))


def test_run_with_deadline_uses_watchdog_when_setitimer_unavailable(monkeypatch) -> None:
    monkeypatch.setattr(comps_snapshot.signal, "setitimer", None, raising=False)

    started = time.monotonic()
    with pytest.raises(_CompScrapeDeadlineExceeded, match="watchdog task exceeded 0.1s"):
        _run_with_deadline(0.1, "watchdog task", lambda: time.sleep(2))

    assert time.monotonic() - started < 1


def test_load_subject_floorplan_summary_treats_missing_file_as_empty(tmp_path) -> None:
    assert load_subject_floorplan_summary(str(tmp_path / "missing.csv")) == []


def test_load_subject_floorplan_summary_accepts_canonical_columns(tmp_path) -> None:
    summary = tmp_path / "floorplan_summary.csv"
    summary.write_text(
        "\n".join(
            [
                "floorplan_code,bed_type,sqft,units,occupied,vacant,occupancy,avg_market_rent,avg_lease_rent,avg_total_rent",
                "p201_A1,1BR,500,76,62,14,0.8158,842.95,875.47,953.78",
            ]
        ),
        encoding="utf-8",
    )

    assert load_subject_floorplan_summary(str(summary)) == [
        {
            "plan_code": "p201_A1",
            "bed_type": "1BR",
            "units": 76,
            "sqft": 500.0,
            "avg_market_rent": 842.95,
        }
    ]
