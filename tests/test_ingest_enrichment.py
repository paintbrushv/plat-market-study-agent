"""Tests for deal enrichment pipeline."""

from __future__ import annotations

from unittest.mock import MagicMock, patch


def test_run_module_success() -> None:
    from etl.ingest_enrichment import _run_module

    def fake_fn(x: int, y: int) -> dict:
        return {"result": x + y}

    result = _run_module("test", fake_fn, x=1, y=2)
    assert result["status"] == "ok"
    assert result["data"] == {"result": 3}


def test_run_module_failure() -> None:
    from etl.ingest_enrichment import _run_module

    def bad_fn() -> None:
        raise ConnectionError("API down")

    result = _run_module("test", bad_fn)
    assert result["status"] == "error"
    assert "API down" in result["error"]


def test_parse_target_year() -> None:
    from etl.ingest_enrichment import _parse_target_year

    assert _parse_target_year("2025-2030") == 2030
    assert _parse_target_year("2024-2029") == 2029
    assert _parse_target_year("2030") == 2030
    assert _parse_target_year("unknown") == 2030  # default fallback


SAMPLE_ENRICHMENT_JSON = {
    "property": "Test Property",
    "address": "123 Main St, Irving, TX 75062",
    "lat": 32.80,
    "lon": -96.62,
    "state_fips": "48",
    "county_fips": "113",
    "county_name": "Dallas",
    "target_year": 2030,
    "generated_at": "2026-04-12T00:00:00+00:00",
    "modules": {
        "environmental": {
            "status": "ok",
            "data": {
                "summary": {"echo_facility_count": 3, "cleanup_site_count": 0},
                "risk_flags": [],
                "echo_facilities": [],
                "cleanup_sites": [],
                "flood_zone": {"zone": "X", "sfha": False},
            },
        },
        "zoning": {
            "status": "ok",
            "data": {
                "city": "Irving",
                "subject_zoning": {"code": "MF-2", "description": "Multifamily"},
                "summary": {"supply_threat": "Moderate", "quality_threat": "Low"},
                "risk_flags": [],
            },
        },
        "projections": {
            "status": "ok",
            "data": {
                "county_demand": {
                    "population": {
                        "base_value": 2145000,
                        "base_year": 2024,
                        "projected": {"base": 2310000},
                        "cagr": 0.012,
                    },
                    "employment": {
                        "base_value": 1050000,
                        "base_year": 2024,
                        "projected": {"base": 1140000},
                        "cagr": 0.013,
                    },
                    "households": {
                        "base_value": 820000,
                        "base_year": 2024,
                        "projected": {"base": 895000},
                        "cagr": 0.014,
                    },
                    "target_year": 2030,
                },
                "tract_growth": [
                    {
                        "geoid": "48113012345",
                        "classification": "Emerging Growth",
                        "unit_cagr_10yr": 0.025,
                        "income_cagr_10yr": 0.018,
                    },
                    {
                        "geoid": "48113012346",
                        "classification": "Stable",
                        "unit_cagr_10yr": 0.005,
                        "income_cagr_10yr": 0.010,
                    },
                    {
                        "geoid": "48113012347",
                        "classification": "Declining",
                        "unit_cagr_10yr": -0.01,
                        "income_cagr_10yr": 0.002,
                    },
                ],
            },
        },
        "schools": {
            "status": "ok",
            "data": {
                "district_name": "Irving ISD",
                "tea_rating": "B",
                "student_achievement": 72,
                "school_progress": 68,
                "closing_gaps": 65,
            },
        },
        "transit": {
            "status": "ok",
            "data": {
                "walkscore": 45,
                "transit_score": 28,
                "bike_score": 35,
                "nearest_rail": "DART Orange Line",
                "nearest_rail_dist_mi": 1.2,
            },
        },
        "migration": {
            "status": "ok",
            "data": {
                "soi": {
                    "net_returns": 4200,
                    "in_avg_agi": 52400,
                    "out_avg_agi": 48100,
                    "income_delta": 4300,
                    "year": 2022,
                },
            },
        },
        "property_tax": {
            "status": "ok",
            "data": {
                "county_name": "Dallas",
                "county_rate": 0.00245,
                "school_district": "Irving ISD",
                "school_rate": 0.0112,
                "total_rate": 0.0245,
                "rate_per_100": 2.45,
            },
        },
        "flood_risk": {
            "status": "ok",
            "data": {"zone": "X", "sfha": False, "zone_subtype": "unshaded", "unmapped": False},
        },
    },
}


def test_render_enrichment_all_ok() -> None:
    from etl.render_enrichment import render_report

    md = render_report(SAMPLE_ENRICHMENT_JSON)

    assert "# Site Profile" in md
    assert "Test Property" in md
    assert "Environmental Screening" in md
    assert "Zoning & Supply Threat" in md
    assert "Demand Projections" in md
    assert "School Quality" in md
    assert "Transit Access" in md
    assert "Migration Flows" in md
    assert "Property Tax" in md
    assert "Flood Risk" in md
    assert "Irving ISD" in md
    assert "DART Orange Line" in md


def test_render_enrichment_with_failures() -> None:
    from etl.render_enrichment import render_report

    data = {
        "property": "Test Property",
        "address": "123 Main St",
        "generated_at": "2026-04-12",
        "target_year": 2030,
        "modules": {
            "environmental": {
                "status": "ok",
                "data": {
                    "summary": {"echo_facility_count": 0, "cleanup_site_count": 0},
                    "risk_flags": [],
                },
            },
            "zoning": {"status": "error", "error": "City not supported"},
            "projections": {"status": "error", "error": "Census API timeout"},
            "schools": {"status": "ok", "data": {"district_name": "Test ISD", "tea_rating": "A"}},
            "transit": {"status": "error", "error": "WALKSCORE_API_KEY not set"},
            "migration": {
                "status": "ok",
                "data": {
                    "soi": {
                        "net_returns": 1000,
                        "in_avg_agi": 50000,
                        "out_avg_agi": 45000,
                        "income_delta": 5000,
                    }
                },
            },
            "property_tax": {"status": "ok", "data": {"total_rate": 0.0245, "rate_per_100": 2.45}},
            "flood_risk": {
                "status": "ok",
                "data": {
                    "zone": "AE",
                    "sfha": True,
                    "zone_subtype": "1% annual flood",
                },
            },
        },
    }

    md = render_report(data)

    # Failed modules show warning
    assert "Data unavailable: City not supported" in md
    assert "Data unavailable: WALKSCORE_API_KEY not set" in md
    assert "Data unavailable: Census API timeout" in md

    # Successful modules render normally
    assert "Environmental Screening" in md
    assert "Test ISD" in md

    # SFHA flood warning rendered
    assert "Special Flood Hazard Area" in md


def test_render_section_order() -> None:
    from etl.render_enrichment import render_report

    md = render_report(SAMPLE_ENRICHMENT_JSON)

    # Spec order: Environmental, Zoning, Projections, Schools, Transit, Migration, Tax, Flood
    env_pos = md.index("Environmental Screening")
    zoning_pos = md.index("Zoning & Supply Threat")
    proj_pos = md.index("Demand Projections")
    schools_pos = md.index("School Quality")
    transit_pos = md.index("Transit Access")
    migration_pos = md.index("Migration Flows")
    tax_pos = md.index("Property Tax")
    flood_pos = md.index("Flood Risk")

    assert env_pos < zoning_pos < proj_pos < schools_pos < transit_pos
    assert transit_pos < migration_pos < tax_pos < flood_pos


SAMPLE_CONFIG = {
    "time_horizon": "2025-2030",
    "notes": {
        "metro_slug": "test_metro",
        "subject_property": {
            "name": "Test Property",
            "address": "123 Main St, Irving, TX 75062",
            "lat": 32.80,
            "lon": -96.62,
        },
    },
    "demographics": {
        "state_fips": "48",
        "county_fips": "113",
    },
}


@patch("etl.ingest_enrichment.screen_site", return_value={"summary": "clean", "risk_flags": []})
@patch("etl.ingest_enrichment.county_name_bulk", return_value={"48113": "Dallas County"})
def test_run_enrichment_environmental(mock_county: MagicMock, mock_env: MagicMock) -> None:
    from etl.ingest_enrichment import run_enrichment

    result = run_enrichment(SAMPLE_CONFIG)

    assert result["property"] == "Test Property"
    assert result["lat"] == 32.80
    assert result["target_year"] == 2030
    assert result["modules"]["environmental"]["status"] == "ok"
    assert result["modules"]["environmental"]["data"]["summary"] == "clean"


@patch("etl.ingest_enrichment.flood_zone", return_value={"zone": "X", "sfha": False})
@patch("etl.ingest_enrichment.effective_tax_rate", return_value={"total_rate": 2.45})
@patch("etl.ingest_enrichment.migration_profile", return_value={"soi": {"net_returns": 4200}})
@patch("etl.ingest_enrichment.transit_profile", return_value={"walkscore": 45})
@patch("etl.ingest_enrichment.school_profile", return_value={"district_name": "Irving ISD"})
@patch("etl.ingest_enrichment.tract_growth_profile")
@patch("etl.ingest_enrichment.county_demand_forecast", return_value={"population": {"cagr": 0.012}})
@patch("etl.ingest_enrichment.zoning_risk", return_value={"summary": "low"})
@patch("etl.ingest_enrichment.screen_site", return_value={"summary": "clean"})
@patch("etl.ingest_enrichment.county_name_bulk", return_value={"48113": "Dallas County"})
def test_run_enrichment_all_modules(
    mock_county: MagicMock,
    mock_env: MagicMock,
    mock_zoning: MagicMock,
    mock_proj_county: MagicMock,
    mock_proj_tract: MagicMock,
    mock_schools: MagicMock,
    mock_transit: MagicMock,
    mock_migration: MagicMock,
    mock_tax: MagicMock,
    mock_flood: MagicMock,
) -> None:
    import pandas as pd
    from etl.ingest_enrichment import run_enrichment

    mock_proj_tract.return_value = pd.DataFrame(
        [{"geoid": "48113012345", "classification": "Emerging Growth"}]
    )

    result = run_enrichment(SAMPLE_CONFIG)

    expected_modules = [
        "environmental",
        "zoning",
        "projections",
        "schools",
        "transit",
        "migration",
        "property_tax",
        "flood_risk",
    ]
    for mod in expected_modules:
        assert mod in result["modules"], f"Missing module: {mod}"
        assert result["modules"][mod]["status"] == "ok"

    # Projections has two subkeys
    assert "county_demand" in result["modules"]["projections"]["data"]
    assert "tract_growth" in result["modules"]["projections"]["data"]


@patch("etl.ingest_enrichment.flood_zone", side_effect=ConnectionError("FEMA API down"))
@patch("etl.ingest_enrichment.effective_tax_rate", return_value={"total_rate": 2.45})
@patch("etl.ingest_enrichment.migration_profile", return_value={"soi": {"net_returns": 4200}})
@patch("etl.ingest_enrichment.transit_profile", side_effect=ValueError("WALKSCORE_API_KEY not set"))
@patch("etl.ingest_enrichment.school_profile", return_value={"district_name": "Irving ISD"})
@patch("etl.ingest_enrichment.tract_growth_profile")
@patch("etl.ingest_enrichment.county_demand_forecast", return_value={"population": {"cagr": 0.012}})
@patch("etl.ingest_enrichment.zoning_risk", return_value={"summary": "low"})
@patch("etl.ingest_enrichment.screen_site", return_value={"summary": "clean"})
@patch("etl.ingest_enrichment.county_name_bulk", return_value={"48113": "Dallas County"})
def test_run_enrichment_partial_failure(
    mock_county: MagicMock,
    mock_env: MagicMock,
    mock_zoning: MagicMock,
    mock_proj_county: MagicMock,
    mock_proj_tract: MagicMock,
    mock_schools: MagicMock,
    mock_transit: MagicMock,
    mock_migration: MagicMock,
    mock_tax: MagicMock,
    mock_flood: MagicMock,
) -> None:
    import pandas as pd
    from etl.ingest_enrichment import run_enrichment

    mock_proj_tract.return_value = pd.DataFrame(
        [{"geoid": "48113012345", "classification": "Stable"}]
    )

    result = run_enrichment(SAMPLE_CONFIG)

    # Transit and flood_risk should fail gracefully
    assert result["modules"]["transit"]["status"] == "error"
    assert "WALKSCORE_API_KEY" in result["modules"]["transit"]["error"]
    assert result["modules"]["flood_risk"]["status"] == "error"
    assert "FEMA API down" in result["modules"]["flood_risk"]["error"]

    # Other modules should succeed
    assert result["modules"]["environmental"]["status"] == "ok"
    assert result["modules"]["zoning"]["status"] == "ok"
    assert result["modules"]["schools"]["status"] == "ok"


@patch("etl.ingest_enrichment.flood_zone", return_value={"zone": "X", "sfha": False})
@patch("etl.ingest_enrichment.effective_tax_rate", return_value={"total_rate": 2.45})
@patch("etl.ingest_enrichment.migration_profile", return_value={"soi": {"net_returns": 4200}})
@patch("etl.ingest_enrichment.transit_profile", return_value={"walkscore": 45})
@patch("etl.ingest_enrichment.school_profile", return_value={"district_name": "Irving ISD"})
@patch("etl.ingest_enrichment.tract_growth_profile")
@patch("etl.ingest_enrichment.county_demand_forecast", return_value={"population": {"cagr": 0.012}})
@patch("etl.ingest_enrichment.zoning_risk", return_value={"summary": "low"})
@patch("etl.ingest_enrichment.screen_site", return_value={"summary": "clean"})
@patch("etl.ingest_enrichment.county_name_bulk", return_value={"48113": "Dallas County"})
def test_write_enrichment_json(
    mock_county: MagicMock,
    mock_env: MagicMock,
    mock_zoning: MagicMock,
    mock_proj_county: MagicMock,
    mock_proj_tract: MagicMock,
    mock_schools: MagicMock,
    mock_transit: MagicMock,
    mock_migration: MagicMock,
    mock_tax: MagicMock,
    mock_flood: MagicMock,
    tmp_path: object,
    monkeypatch: object,
) -> None:
    import json

    import pandas as pd
    from etl.ingest_enrichment import run_enrichment, write_enrichment

    mock_proj_tract.return_value = pd.DataFrame(
        [{"geoid": "48113012345", "classification": "Emerging Growth"}]
    )

    # Use tmp_path as output base
    monkeypatch.chdir(tmp_path)

    result = run_enrichment(SAMPLE_CONFIG)
    out_path = write_enrichment(result, "test_metro")

    assert out_path.exists()
    with open(out_path) as f:
        loaded = json.load(f)

    assert loaded["property"] == "Test Property"
    assert len(loaded["modules"]) == 8
    tract_growth = loaded["modules"]["projections"]["data"]["tract_growth"]
    assert tract_growth[0]["classification"] == "Emerging Growth"
