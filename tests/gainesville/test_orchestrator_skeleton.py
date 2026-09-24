from __future__ import annotations

from pathlib import Path

import pytest
from etl.gainesville.run_weekly import load_catchment_from_yaml, run_weekly


def test_orchestrator_runs_with_no_sources(tmp_path: Path) -> None:
    db_path = tmp_path / "g.duckdb"
    lock_path = tmp_path / "g.lock"
    runs_dir = tmp_path / "runs"
    result = run_weekly(
        db_path=db_path,
        lock_path=lock_path,
        runs_dir=runs_dir,
        catchment=None,  # type: ignore[arg-type]
        sources=[],
    )
    assert result.run_id.startswith("20")
    assert result.status in {"success", "partial"}
    assert result.sources_ok == []
    assert result.sources_failed == []


def test_load_catchment_from_yaml_valid(tmp_path: Path) -> None:
    """load_catchment_from_yaml returns a Catchment matching the YAML catchment block."""
    cfg = tmp_path / "test_config.yaml"
    cfg.write_text(
        "catchment:\n"
        "  county_fips: '48097'\n"
        "  zip_codes: ['76240', '76252']\n"
        "  bbox: [33.5, -97.4, 33.8, -96.8]\n"
        "  city_limits_geojson: 'data/local/gainesville_city_limits.geojson'\n",
        encoding="utf-8",
    )
    catchment = load_catchment_from_yaml(cfg)
    assert catchment.county_fips == "48097"
    assert catchment.zip_codes == ("76240", "76252")
    assert catchment.bbox == (33.5, -97.4, 33.8, -96.8)
    assert catchment.city_limits_geojson_path == "data/local/gainesville_city_limits.geojson"


def test_load_catchment_from_yaml_missing_file(tmp_path: Path) -> None:
    """load_catchment_from_yaml raises FileNotFoundError for a non-existent path."""
    with pytest.raises(FileNotFoundError, match="config not found"):
        load_catchment_from_yaml(tmp_path / "no_such_file.yaml")


def test_load_catchment_from_yaml_missing_block(tmp_path: Path) -> None:
    """load_catchment_from_yaml raises ValueError when the catchment block is absent."""
    cfg = tmp_path / "no_catchment.yaml"
    cfg.write_text("metro: 'test'\n", encoding="utf-8")
    with pytest.raises(ValueError, match="missing 'catchment' block"):
        load_catchment_from_yaml(cfg)


def test_load_catchment_from_yaml_missing_keys(tmp_path: Path) -> None:
    """load_catchment_from_yaml raises ValueError listing the missing required keys."""
    cfg = tmp_path / "partial.yaml"
    cfg.write_text(
        "catchment:\n"
        "  county_fips: '48097'\n"
        "  zip_codes: ['76240']\n",  # bbox missing
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="missing keys"):
        load_catchment_from_yaml(cfg)


def test_orchestrator_refuses_when_locked(tmp_path: Path) -> None:
    db_path = tmp_path / "g.duckdb"
    lock_path = tmp_path / "g.lock"
    runs_dir = tmp_path / "runs"
    lock_path.write_text("99999999\n9999-01-01T00:00:00+00:00\n")  # fake fresh-ish
    # The fake pid 99999999 is dead, but the recent timestamp shouldn't matter
    # because is_stale also checks pid liveness — dead pid -> stale -> claimable.
    # So instead use the current process's pid with a fresh timestamp.
    import datetime as dt
    import os
    lock_path.write_text(f"{os.getpid()}\n{dt.datetime.now(dt.UTC).isoformat()}\n")
    result = run_weekly(
        db_path=db_path,
        lock_path=lock_path,
        runs_dir=runs_dir,
        catchment=None,  # type: ignore[arg-type]
        sources=[],
    )
    assert result.status == "lock_busy"
