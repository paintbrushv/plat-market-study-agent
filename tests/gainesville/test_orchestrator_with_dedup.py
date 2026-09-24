from __future__ import annotations

from pathlib import Path

from etl.gainesville.dataclasses import Catchment
from etl.gainesville.db import apply_schema, connect
from etl.gainesville.run_weekly import build_sources_from_config, run_weekly

FIXTURES = Path(__file__).parent / "fixtures"


def _fake_fetch(url: str, timeout: int = 20) -> str:
    if "search/apa" in url:
        return (FIXTURES / "craigslist_search.html").read_text(encoding="utf-8")
    return (FIXTURES / "craigslist_post.html").read_text(encoding="utf-8")


def test_full_run_creates_canonicals_and_review_csv(tmp_path: Path) -> None:
    catchment = Catchment("48097", ("76240",), (33.5, -97.4, 33.8, -96.8), None)
    config = {
        "sources": {"craigslist": {
            "enabled": True, "regions": ["dallas"], "keywords": ["gainesville"],
            "_test_fetch_fn": _fake_fetch, "_test_delay_s": 0.0,
        }},
        "paths": {
            "parquet_history_mf": str(tmp_path / "mf_hist"),
            "parquet_history_sfr": str(tmp_path / "sfr_hist"),
            "ops_dedup_pending": str(tmp_path / "_ops"),
        },
    }
    db_path = tmp_path / "g.duckdb"
    sources = build_sources_from_config(config, db_path=db_path)
    summary = run_weekly(
        db_path=db_path, lock_path=tmp_path / "g.lock",
        runs_dir=tmp_path / "runs", catchment=catchment, sources=sources,
        config=config,
    )
    assert summary.status == "success"
    conn = connect(db_path)
    apply_schema(conn)
    n_canonicals = conn.execute("SELECT COUNT(*) FROM canonical_listings").fetchone()[0]
    assert n_canonicals >= 1
    review_csv = tmp_path / "_ops" / f"dedup_review_{summary.run_id}.csv"
    assert review_csv.exists()
