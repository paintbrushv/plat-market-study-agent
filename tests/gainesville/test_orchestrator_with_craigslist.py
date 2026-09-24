from __future__ import annotations

import json
from pathlib import Path

from etl.gainesville.dataclasses import Catchment
from etl.gainesville.db import apply_schema, connect
from etl.gainesville.run_weekly import build_sources_from_config, run_weekly

FIXTURES = Path(__file__).parent / "fixtures"


def _fake_fetch(url: str, timeout: int = 20) -> str:
    if "search/apa" in url:
        return (FIXTURES / "craigslist_search.html").read_text(encoding="utf-8")
    return (FIXTURES / "craigslist_post.html").read_text(encoding="utf-8")


def _fake_fetch_with_for_sale(url: str, timeout: int = 20) -> str:
    """Return a for-sale-disguised listing for every post URL."""
    if "search/apa" in url:
        return (FIXTURES / "craigslist_search.html").read_text(encoding="utf-8")
    return (FIXTURES / "craigslist_post_for_sale.html").read_text(encoding="utf-8")


def test_craigslist_run_writes_observations_and_snapshot(tmp_path: Path) -> None:
    catchment = Catchment("48097", ("76240",), (33.5, -97.4, 33.8, -96.8), None)
    config = {
        "sources": {
            "craigslist": {
                "enabled": True,
                "regions": ["dallas"],
                "keywords": ["gainesville", "76240"],
                "_test_fetch_fn": _fake_fetch,
                "_test_delay_s": 0.0,
            },
        },
        "paths": {
            "parquet_history_mf": str(tmp_path / "mf_hist"),
            "parquet_history_sfr": str(tmp_path / "sfr_hist"),
        },
    }
    db_path = tmp_path / "g.duckdb"
    sources = build_sources_from_config(config, db_path=db_path)
    summary = run_weekly(
        db_path=db_path,
        lock_path=tmp_path / "g.lock",
        runs_dir=tmp_path / "runs",
        catchment=catchment,
        sources=sources,
    )
    assert summary.status == "success"
    assert "craigslist" in summary.sources_ok
    assert summary.new_observations >= 1
    conn = connect(db_path)
    apply_schema(conn)
    n = conn.execute(
        "SELECT COUNT(*) FROM raw_observations WHERE source='craigslist'"
    ).fetchone()[0]
    assert n >= 1
    snapshots = list((tmp_path / "sfr_hist" / summary.run_id).glob("craigslist.parquet"))
    assert len(snapshots) == 1


def test_for_sale_listing_is_dropped_and_not_written_to_db(tmp_path: Path) -> None:
    """A Craigslist post with Turbotenant link + mortgage estimate must be dropped
    entirely — zero rows in raw_observations and dropped_for_sale=1 in the ops JSONL."""
    catchment = Catchment("48097", ("76240",), (33.5, -97.4, 33.8, -96.8), None)
    config = {
        "sources": {
            "craigslist": {
                "enabled": True,
                "regions": ["dallas"],
                "keywords": ["gainesville", "76240"],
                "_test_fetch_fn": _fake_fetch_with_for_sale,
                "_test_delay_s": 0.0,
            },
        },
        "paths": {
            "parquet_history_mf": str(tmp_path / "mf_hist"),
            "parquet_history_sfr": str(tmp_path / "sfr_hist"),
        },
    }
    db_path = tmp_path / "g.duckdb"
    sources = build_sources_from_config(config, db_path=db_path)
    runs_dir = tmp_path / "runs"
    summary = run_weekly(
        db_path=db_path,
        lock_path=tmp_path / "g.lock",
        runs_dir=runs_dir,
        catchment=catchment,
        sources=sources,
    )
    assert summary.status == "success"

    # No for-sale listings should be written to raw_observations.
    conn = connect(db_path)
    apply_schema(conn)
    n_craigslist = conn.execute(
        "SELECT COUNT(*) FROM raw_observations WHERE source='craigslist'"
    ).fetchone()[0]
    assert n_craigslist == 0, (
        f"Expected 0 craigslist observations after for-sale filter, got {n_craigslist}"
    )

    # The ops JSONL must record dropped_for_sale >= 1 for the craigslist source_done event.
    run_jsonl = runs_dir / f"{summary.run_id}.jsonl"
    assert run_jsonl.exists(), f"ops JSONL not found: {run_jsonl}"
    source_done_events = [
        json.loads(line)
        for line in run_jsonl.read_text(encoding="utf-8").splitlines()
        if json.loads(line).get("event") == "source_done"
        and json.loads(line).get("source") == "craigslist"
    ]
    assert source_done_events, "No craigslist source_done event found in ops JSONL"
    cl_event = source_done_events[0]
    dropped = cl_event.get("diagnostics", {}).get("dropped_for_sale", 0)
    assert dropped >= 1, (
        f"Expected dropped_for_sale >= 1 in craigslist source_done, got {dropped}. "
        f"Full event: {cl_event}"
    )
