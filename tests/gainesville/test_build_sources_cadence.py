"""Tests for cadence-aware source filtering and ZORI zip override in build_sources_from_config."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from etl.gainesville.dataclasses import Catchment, CollectionStatus
from etl.gainesville.db import apply_schema, connect
from etl.gainesville.run_weekly import RunSummary, build_sources_from_config, main

FIXTURES = Path(__file__).parent / "fixtures"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

REAL_CONFIG = Path("agents/configs/gainesville_tx.yaml")


def _make_config_with_cadence(
    zori_enabled: bool = True,
    cad_enabled: bool = True,
    zori_csv: str | None = None,
) -> dict:
    """Minimal config dict with a cadence block matching the real YAML layout."""
    sources: dict = {}
    if zori_enabled:
        s: dict = {"enabled": True}
        if zori_csv:
            s["csv_path_override"] = zori_csv
        sources["zori"] = s
    else:
        sources["zori"] = {"enabled": False}
    if cad_enabled:
        sources["cooke_cad_import"] = {
            "enabled": True,
            "klement_db_path": "/fake/klement.db",
            "asset_class_filter": {
                "property_types": ["residential_mf"],
                "owner_name_keywords": ["APARTMENTS", "APTS"],
            },
        }
    else:
        sources["cooke_cad_import"] = {"enabled": False}
    return {
        "sources": sources,
        "cadence": {
            "weekly_sources": ["apartments_com", "zillow", "craigslist", "property_direct"],
            "monthly_sources": ["zori"],
            "quarterly_sources": ["cooke_cad_import"],
        },
    }


# ---------------------------------------------------------------------------
# Finding 1: cadence filtering
# ---------------------------------------------------------------------------


def test_weekly_cadence_excludes_zori_and_cad(tmp_path: Path) -> None:
    """Weekly run must not include zori (monthly) or cooke_cad_import (quarterly)."""
    import yaml

    if not REAL_CONFIG.exists():
        pytest.skip("real gainesville_tx.yaml not present")
    config = yaml.safe_load(REAL_CONFIG.read_text(encoding="utf-8"))
    sources = build_sources_from_config(config, db_path=tmp_path / "g.duckdb", cadence="weekly")
    names = [s.name for s in sources]
    assert "zori" not in names, f"zori should not run on weekly cadence; got: {names}"
    assert "cooke_cad_import" not in names, (
        f"cooke_cad_import should not run on weekly cadence; got: {names}"
    )


def test_monthly_cadence_includes_zori(tmp_path: Path) -> None:
    """Monthly run must include zori."""
    config = _make_config_with_cadence(
        zori_enabled=True,
        cad_enabled=False,
        zori_csv=str(FIXTURES / "zori_sample.csv"),
    )
    sources = build_sources_from_config(config, db_path=tmp_path / "g.duckdb", cadence="monthly")
    names = [s.name for s in sources]
    assert "zori" in names, f"zori should be included on monthly cadence; got: {names}"
    assert "cooke_cad_import" not in names, (
        f"cooke_cad_import should not be on monthly cadence; got: {names}"
    )


def test_quarterly_cadence_includes_cad(tmp_path: Path) -> None:
    """Quarterly run must include cooke_cad_import."""
    config = _make_config_with_cadence(zori_enabled=False, cad_enabled=True)
    sources = build_sources_from_config(config, db_path=tmp_path / "g.duckdb", cadence="quarterly")
    names = [s.name for s in sources]
    assert "cooke_cad_import" in names, (
        f"cooke_cad_import should be included on quarterly cadence; got: {names}"
    )
    assert "zori" not in names, f"zori should not run on quarterly cadence; got: {names}"


def test_no_cadence_block_includes_all_enabled(tmp_path: Path) -> None:
    """When no cadence block is in the config, all enabled sources are included."""
    config = {
        "sources": {
            "zori": {
                "enabled": True,
                "csv_path_override": str(FIXTURES / "zori_sample.csv"),
            },
            "cooke_cad_import": {"enabled": False},
        },
        # no "cadence" key — old config style
    }
    sources = build_sources_from_config(config, db_path=tmp_path / "g.duckdb", cadence="weekly")
    names = [s.name for s in sources]
    assert "zori" in names, f"zori should be included when no cadence block; got: {names}"


def test_main_cadence_flag_weekly_excludes_zori(tmp_path: Path) -> None:
    """--cadence weekly passed to main() must not include zori in the sources list."""
    if not REAL_CONFIG.exists():
        pytest.skip("real gainesville_tx.yaml not present")

    captured: list = []

    def _fake_run_weekly(**kwargs: object) -> RunSummary:
        captured.append(kwargs["sources"])
        return RunSummary("r1", "success", [], [], 0)

    config_path = tmp_path / "gainesville_tx.yaml"
    config_path.write_text(REAL_CONFIG.read_text(encoding="utf-8"), encoding="utf-8")

    with patch("etl.gainesville.run_weekly.run_weekly", side_effect=_fake_run_weekly):
        rc = main([
            "--db", str(tmp_path / "g.duckdb"),
            "--lock", str(tmp_path / ".g.lock"),
            "--runs-dir", str(tmp_path / "runs"),
            "--config", str(config_path),
            "--cadence", "weekly",
        ])

    assert rc == 0
    assert len(captured) == 1
    names = [s.name for s in captured[0]]
    assert "zori" not in names, f"--cadence weekly must exclude zori; got: {names}"
    assert "cooke_cad_import" not in names, (
        f"--cadence weekly must exclude cooke_cad_import; got: {names}"
    )


# ---------------------------------------------------------------------------
# Finding 2: ZORI zip override
# ---------------------------------------------------------------------------


def test_zori_zip_override_narrows_scope(tmp_path: Path) -> None:
    """When zori.zips is set to ['76240'], only that zip lands in zori_history
    even though the catchment contains 3 zips."""
    db_path = tmp_path / "g.duckdb"
    conn = connect(db_path)
    apply_schema(conn)

    config = {
        "sources": {
            "zori": {
                "enabled": True,
                "csv_path_override": str(FIXTURES / "zori_sample.csv"),
                "zips": ["76240"],
            },
            "cooke_cad_import": {"enabled": False},
        },
        # No cadence block — include all enabled regardless
    }
    sources = build_sources_from_config(config, db_path=db_path)
    assert len(sources) == 1
    zori_src = sources[0]
    assert zori_src.name == "zori"

    # Catchment has 3 zips; the zori source should only ingest 76240.
    catchment = Catchment(
        county_fips="48097",
        zip_codes=("76240", "76252", "76253"),
        bbox=(33.5, -97.4, 33.8, -96.8),
        city_limits_geojson_path=None,
    )
    result = zori_src.collect(catchment, run_id="test-run-001")
    assert result.status == CollectionStatus.OK, f"Expected OK, got {result.status}"

    rows = conn.execute(
        "SELECT zip FROM zori_history ORDER BY zip"
    ).fetchall()
    zips_found = {r[0] for r in rows}
    assert zips_found == {"76240"}, (
        f"Expected only '76240' in zori_history due to zips override, got: {zips_found}"
    )
