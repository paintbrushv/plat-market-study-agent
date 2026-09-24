from __future__ import annotations

from pathlib import Path

from etl.gainesville.dataclasses import Catchment
from etl.gainesville.db import apply_schema, connect
from etl.gainesville.run_weekly import build_sources_from_config, run_weekly

FIXTURES = Path(__file__).parent / "fixtures"


def test_property_direct_persists_observations(tmp_path: Path) -> None:
    catchment = Catchment("48097", ("76240",), (33.5, -97.4, 33.8, -96.8), None)
    fp_html = (FIXTURES / "property_direct_floorplans.html").read_text(encoding="utf-8")
    config = {
        "sources": {
            "property_direct": {
                "enabled": True,
                "sites": [{
                    "name": "Tower View",
                    "url": "https://example.com/fp",
                    "address": "500 Pine Drive, Gainesville, TX 76240",
                    "selectors": {
                        "floorplan_card": ".fp-card",
                        "name": ".fp-name",
                        "rent": ".fp-rent",
                        "beds": ".fp-beds",
                        "baths": ".fp-baths",
                        "sqft": ".fp-sqft",
                    },
                }],
                "_test_html_overrides": {
                    "https://example.com/fp": fp_html,
                },
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
        db_path=db_path, lock_path=tmp_path / "g.lock",
        runs_dir=tmp_path / "runs", catchment=catchment, sources=sources,
        config=config,
    )
    assert summary.status == "success"
    conn = connect(db_path)
    apply_schema(conn)
    n = conn.execute(
        "SELECT COUNT(*) FROM raw_observations WHERE source='property_direct'"
    ).fetchone()[0]
    assert n == 2  # 2 floorplans in fixture
