from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from etl.gainesville.dataclasses import Catchment
from etl.gainesville.run_weekly import RunSummary, build_sources_from_config, main, run_weekly

FIXTURES = Path(__file__).parent / "fixtures"


def test_main_constructs_sources_from_yaml(tmp_path: Path) -> None:
    """main() must call build_sources_from_config — not pass a hard-coded empty list."""
    # Write a minimal config that enables zori (with csv override so no network hit).
    config_text = f"""
catchment:
  county_fips: "48097"
  zip_codes: ["76240"]
  bbox: [33.5, -97.4, 33.8, -96.8]
sources:
  zori:
    enabled: true
    csv_path_override: "{FIXTURES / "zori_sample.csv"}"
  cooke_cad_import:
    enabled: false
"""
    config_path = tmp_path / "gainesville_tx.yaml"
    config_path.write_text(config_text, encoding="utf-8")
    db_path = tmp_path / "g.duckdb"
    lock_path = tmp_path / ".g.lock"
    runs_dir = tmp_path / "runs"

    captured: list = []

    def _fake_run_weekly(**kwargs: object) -> RunSummary:
        captured.append(kwargs["sources"])
        return RunSummary("r1", "success", ["zori"], [], 0)

    with patch("etl.gainesville.run_weekly.run_weekly", side_effect=_fake_run_weekly):
        rc = main([
            "--db", str(db_path),
            "--lock", str(lock_path),
            "--runs-dir", str(runs_dir),
            "--config", str(config_path),
        ])

    assert rc == 0, "main() should return 0 on success"
    assert len(captured) == 1, "run_weekly should be called exactly once"
    sources = captured[0]
    assert len(sources) == 1, f"Expected 1 source (zori), got {[s.name for s in sources]}"
    assert sources[0].name == "zori", f"Expected zori source, got {sources[0].name!r}"


def test_orchestrator_runs_zori_only(tmp_path: Path) -> None:
    catchment = Catchment("48097", ("76240", "76252"), (33.5, -97.4, 33.8, -96.8), None)
    config = {
        "sources": {
            "zori": {"enabled": True, "csv_path_override": str(FIXTURES / "zori_sample.csv")},
            "cooke_cad_import": {"enabled": False},
        },
    }
    sources = build_sources_from_config(config, db_path=tmp_path / "g.duckdb")
    summary = run_weekly(
        db_path=tmp_path / "g.duckdb",
        lock_path=tmp_path / "g.lock",
        runs_dir=tmp_path / "runs",
        catchment=catchment,
        sources=sources,
    )
    assert summary.status == "success"
    assert "zori" in summary.sources_ok
