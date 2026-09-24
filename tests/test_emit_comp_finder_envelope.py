from __future__ import annotations

import json
from pathlib import Path

import yaml

from agents.runners.emit_comp_finder_envelope import build_comp_finder_response
from plat_agent.contracts.domain.market_study import CompsArtifact


def _write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def test_comp_tier_from_notes_maps_only_current_schema_tiers() -> None:
    from agents.runners.emit_comp_finder_envelope import _comp_tier_from_notes

    assert _comp_tier_from_notes("Tier 1 - Premium; broker OM seed") == "premium"
    assert _comp_tier_from_notes("Tier 2 - Mid-Market; direct parser") == "mid_market"
    assert _comp_tier_from_notes("Tier 3 - Value; OM seed only") == "value"
    assert _comp_tier_from_notes("Broker OM rent comp") is None
    assert _comp_tier_from_notes("Omitted nearby South Congress peer") is None


def test_build_comp_finder_response_ok(tmp_path: Path) -> None:
    deal_root = tmp_path / "belle_mor"
    run_id = "run_001"
    _write(
        deal_root / "outputs" / run_id / "market_study" / "comps_cohort_grouped.json",
        {
            "subject": {"name": "Belle Mor"},
            "as_of": "2026-05-08",
            "status": "ok",
            "comps_by_cohort": {
                "1BR_1.0BA_700sf": [
                    {
                        "property_name": "Comp A",
                        "bedrooms": 1,
                        "bathrooms": 1.0,
                        "sqft": 700.0,
                        "asking_rent": 1200.0,
                        "rent_per_sqft": 1.71,
                        "source": "site-a",
                    },
                    {
                        "property_name": "Comp B",
                        "bedrooms": 1,
                        "bathrooms": 1.0,
                        "sqft": 705.0,
                        "asking_rent": 1210.0,
                        "rent_per_sqft": 1.72,
                        "source": "site-b",
                    },
                    {
                        "property_name": "Comp C",
                        "bedrooms": 1,
                        "bathrooms": 1.0,
                        "sqft": 710.0,
                        "asking_rent": 1220.0,
                        "rent_per_sqft": 1.72,
                        "source": "site-c",
                    },
                ]
            },
            "methodology_notes": ["Asking rents only."],
        },
    )
    _write(
        deal_root / "outputs" / run_id / "market_study" / "_provenance.json",
        {
            "scraper_version": "market-study-agent/comp-finder V1.5",
            "config_used": "agents/configs/huntsville_al_belle_mor.yaml",
            "source_breakdown": {
                "om_extraction": {
                    "source": "outputs/run_001/raw_inputs/Belle Mor OM.pdf",
                    "note": "Broker comp set.",
                }
            },
        },
    )
    _write(
        deal_root / "outputs" / run_id / "comps" / "comps.json",
        {
            "subject": {"address": "240 Kirby Lane"},
            "as_of": "2026-05-08",
            "comps": [
                {"name": "Comp A", "unit_types": [{"unit_type": "1BR", "sqft": 700.0, "face_rent": 1200.0, "rent_psf": 1.71}]},
                {"name": "Comp B", "unit_types": [{"unit_type": "1BR", "sqft": 705.0, "face_rent": 1210.0, "rent_psf": 1.72}]},
                {"name": "Comp C", "unit_types": [{"unit_type": "1BR", "sqft": 710.0, "face_rent": 1220.0, "rent_psf": 1.72}]},
            ],
        },
    )

    response = build_comp_finder_response(
        deal_root=deal_root,
        deal_slug="belle_mor",
        run_id=run_id,
    )

    assert response.status == "ok"
    assert response.error is None
    assert response.payload is not None
    assert response.payload["comps_relative"] == f"outputs/{run_id}/comps/comps.json"
    assert response.sanity_flags == []


def test_build_comp_finder_response_errors_when_comps_json_missing(tmp_path: Path) -> None:
    deal_root = tmp_path / "belle_mor"
    run_id = "run_001"
    _write(
        deal_root / "outputs" / run_id / "market_study" / "comps_cohort_grouped.json",
        {
            "subject": {"name": "Belle Mor"},
            "status": "ok",
            "comps_by_cohort": {"1BR_1.0BA_700sf": []},
            "methodology_notes": [],
        },
    )

    response = build_comp_finder_response(
        deal_root=deal_root,
        deal_slug="belle_mor",
        run_id=run_id,
    )

    assert response.status == "error"
    assert response.payload is None
    assert response.error is not None
    assert response.error.code == "missing_or_invalid_artifact"


def test_build_comp_finder_response_salvages_missing_artifacts_from_tracking(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import agents.runners.emit_comp_finder_envelope as helper

    monkeypatch.setattr(helper, "REPO_ROOT", tmp_path)

    deal_root = tmp_path / "the_place_at_briarcrest"
    run_id = "run_002"
    intake = deal_root / "outputs" / run_id / "intake"
    intake.mkdir(parents=True, exist_ok=True)
    intake.joinpath("canonical_deal.json").write_text(
        json.dumps(
            {
                "metadata": {
                    "deal_id": "The Place at Briarcrest",
                    "address": "1330 Mac Arthur Drive, Carrollton, TX 75007",
                    "market": "dallas_tx",
                    "as_of_date": "2026-06-01",
                },
                "unit_cohorts": [
                    {
                        "cohort_id": "bc_q1",
                        "unit_type": "bc_Q1",
                        "unit_count": 50,
                        "sqft": 860,
                        "bedrooms": 2,
                        "bathrooms": 1.0,
                    },
                    {
                        "cohort_id": "bc_b1",
                        "unit_type": "bc_B1",
                        "unit_count": 102,
                        "sqft": 925,
                        "bedrooms": 2,
                        "bathrooms": 2.0,
                    },
                ],
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    cfg = {
        "notes": {
            "subject_property": {
                "name": "The Place at Briarcrest",
                "address": "1330 Mac Arthur Drive, Carrollton, TX 75007",
            }
        },
        "comp_monitoring": {
            "subjects": [
                {
                    "floorplan_summary_csv": "reports/dallas-tx/the-place-at-briarcrest/rent-roll/clean/floorplan_summary.csv"
                }
            ]
        },
    }
    config_path = tmp_path / "agents" / "configs" / "dallas_tx_the_place_at_briarcrest.yaml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(yaml.safe_dump(cfg), encoding="utf-8")

    property_dir = tmp_path / "reports" / "dallas-tx" / "the-place-at-briarcrest"
    tracking = property_dir / "comps" / "tracking"
    tracking.mkdir(parents=True, exist_ok=True)
    (property_dir / "rent-roll" / "clean").mkdir(parents=True, exist_ok=True)
    (property_dir / "rent-roll" / "clean" / "floorplan_summary.csv").write_text("x\n", encoding="utf-8")
    (tracking / "property_database.csv").write_text(
        "\n".join(
            [
                "property,address,distance_mi,units,year_built,stories,owner,management_company,ownership_type,renovation_status,renovation_year,condition_rating,cap_rate_est,last_sale_date,last_sale_price,last_sale_ppu,notes",
                "The Place at Briarcrest,1330 Mac Arthur Drive,0.0,238,1984,2,Unknown,MC Companies,Private,Partial,2016,B,,, , ,Subject property",
                "Comp A,Carrollton TX,1.2,120,1985,2,Unknown,Unknown,Private,Partial,2020,B+,,,,,Primary comp",
                "Comp B,Carrollton TX,1.5,140,1986,2,Unknown,Unknown,Private,Original,,B,,,,,Secondary comp",
                "Comp C,Carrollton TX,1.8,160,1987,2,Unknown,Unknown,Private,Original,,B-,,,,,Third comp",
            ]
        ),
        encoding="utf-8",
    )
    (tracking / "rent_history.csv").write_text(
        "\n".join(
            [
                "date,property,address,unit_type,beds,baths,sq_ft,face_rent,effective_rent,rent_psf,units_available,mom_change,yoy_change,notes",
                "2026-05-08,Comp A,Carrollton TX,2BR,2,1,870,$1500,$1490,$1.71,4,,,",
                "2026-05-08,Comp A,Carrollton TX,2BR,2,2,930,$1650,$1640,$1.77,3,,,",
                "2026-05-08,Comp B,Carrollton TX,2BR,2,1,855,$1480,$1480,$1.73,2,,,",
                "2026-05-08,Comp B,Carrollton TX,2BR,2,2,920,$1630,$1620,$1.76,1,,,",
                "2026-05-08,Comp C,Carrollton TX,2BR,2,1,880,$1510,$1500,$1.70,2,,,",
                "2026-05-08,Comp C,Carrollton TX,2BR,2,2,940,$1665,$1655,$1.77,2,,,",
            ]
        ),
        encoding="utf-8",
    )
    (tracking / "concession_history.csv").write_text(
        "\n".join(
            [
                "date,property,address,unit_type,face_rent,concession_type,concession_value,free_months,effective_rent,lease_requirement,notes",
                "2026-05-08,Comp A,Carrollton TX,All,$1500,None,0,0,$1490,12,",
                "2026-05-08,Comp B,Carrollton TX,All,$1480,None,0,0,$1480,12,",
                "2026-05-08,Comp C,Carrollton TX,All,$1510,None,0,0,$1500,12,",
            ]
        ),
        encoding="utf-8",
    )
    snapshot_dir = tmp_path / "data" / "public" / "processed" / "comps"
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    (snapshot_dir / "2026-05-08_the_place_at_briarcrest_comps_snapshot.json").write_text(
        json.dumps({"run_date": "2026-05-08", "comps": []}),
        encoding="utf-8",
    )

    response = build_comp_finder_response(
        deal_root=deal_root,
        deal_slug="the_place_at_briarcrest",
        run_id=run_id,
    )

    assert response.status == "needs_analyst_input"
    assert response.payload is not None
    assert (deal_root / "outputs" / run_id / "market_study" / "comps_cohort_grouped.json").exists()
    assert (deal_root / "outputs" / run_id / "comps" / "comps.json").exists()
    assert (deal_root / "outputs" / run_id / "market_study" / "_provenance.json").exists()


def test_build_comp_finder_response_rebuilds_existing_salvage_with_supplemental_comps(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import agents.runners.emit_comp_finder_envelope as helper

    monkeypatch.setattr(helper, "REPO_ROOT", tmp_path)

    deal_root = tmp_path / "ladera"
    run_id = "run_001"
    intake = deal_root / "outputs" / run_id / "intake"
    intake.mkdir(parents=True, exist_ok=True)
    intake.joinpath("canonical_deal.json").write_text(
        json.dumps(
            {
                "metadata": {
                    "deal_id": "Ladera",
                    "address": "7500 S IH-35, Austin, TX 78745",
                    "market": "austin_tx",
                    "as_of_date": "2026-05-13",
                },
                "unit_cohorts": [
                    {
                        "cohort_id": "one_br",
                        "unit_type": "A1",
                        "unit_count": 100,
                        "sqft": 700,
                        "bedrooms": 0,
                        "bathrooms": 1.0,
                    }
                ],
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    config_path = tmp_path / "agents" / "configs" / "austin_tx_ladera.yaml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        yaml.safe_dump(
            {
                "notes": {
                    "subject_property": {
                        "name": "Ladera",
                        "address": "7500 S IH-35, Austin, TX 78745",
                    }
                },
                "comp_monitoring": {
                    "subjects": [
                        {
                            "floorplan_summary_csv": "reports/austin-tx/ladera/rent-roll/clean/floorplan_summary.csv"
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )

    property_dir = tmp_path / "reports" / "austin-tx" / "ladera"
    tracking = property_dir / "comps" / "tracking"
    tracking.mkdir(parents=True, exist_ok=True)
    (property_dir / "rent-roll" / "clean").mkdir(parents=True, exist_ok=True)
    (property_dir / "rent-roll" / "clean" / "floorplan_summary.csv").write_text("x\n", encoding="utf-8")
    (tracking / "property_database.csv").write_text(
        "\n".join(
            [
                "property,address,distance_mi,units,year_built,stories,owner,management_company,ownership_type,renovation_status,renovation_year,condition_rating,last_sale_date,last_sale_price,last_sale_ppu,cap_rate_est,notes",
                "Ladera,7500 S IH-35,0,308,2013,3,,,,,,,,,,,Subject property",
                "Broker Comp,Austin TX,1.0,300,2020,4,,,private,original,,A,,,,,Broker OM rent comp",
                "Supplemental Comp,Austin TX,1.2,262,2020,4,,,private,original,,A-,,,,,Omitted nearby South Congress peer",
            ]
        ),
        encoding="utf-8",
    )
    (tracking / "rent_history.csv").write_text(
        "\n".join(
            [
                "date,property,address,unit_type,beds,baths,sq_ft,face_rent,effective_rent,rent_psf,units_available,mom_change,yoy_change,notes",
                "2026-05-13,Broker Comp,Austin TX,1BR,1,1,703,1340,1340,1.91,,,,OM one-bedroom comparison.",
                "2026-05-13,Supplemental Comp,Austin TX,1BR,1,1,700,1217,1217,1.74,5,,,Direct salvage from spaces_rendered_html cache.",
            ]
        ),
        encoding="utf-8",
    )
    (tracking / "concession_history.csv").write_text(
        "date,property,address,unit_type,face_rent,concession_type,concession_value,free_months,effective_rent,lease_requirement,notes\n",
        encoding="utf-8",
    )

    stale_grouped = deal_root / "outputs" / run_id / "market_study" / "comps_cohort_grouped.json"
    stale_comps = deal_root / "outputs" / run_id / "comps" / "comps.json"
    stale_provenance = deal_root / "outputs" / run_id / "market_study" / "_provenance.json"
    _write(
        stale_grouped,
        {
            "subject": {"name": "Ladera"},
            "status": "needs_analyst_input",
            "comps_by_cohort": {"1BR_1.0BA_700sf": []},
            "methodology_notes": [],
        },
    )
    _write(
        stale_comps,
        {"subject": {}, "as_of": "2026-05-13", "comps": []},
    )
    _write(
        stale_provenance,
        {
            "scraper_version": "market-study-agent/comp-finder salvage",
            "repo_native_outputs": {},
        },
    )

    response = build_comp_finder_response(
        deal_root=deal_root,
        deal_slug="ladera",
        run_id=run_id,
    )

    assert response.payload is not None
    comps_payload = json.loads(stale_comps.read_text(encoding="utf-8"))
    assert [comp["name"] for comp in comps_payload["comps"]] == ["Broker Comp", "Supplemental Comp"]
    supplemental = comps_payload["comps"][1]
    assert supplemental["tier"] is None
    CompsArtifact.model_validate(comps_payload)
    assert supplemental["unit_types"][0]["evidence_quality"] == "direct_cached"
    grouped_payload = json.loads(stale_grouped.read_text(encoding="utf-8"))
    assert "1BR_1.0BA_700sf" in grouped_payload["comps_by_cohort"]
    sources = {entry["source"] for entry in grouped_payload["comps_by_cohort"]["1BR_1.0BA_700sf"]}
    assert "direct_cached" in sources
    assert "om_floorplan" in sources


def test_build_comp_finder_response_salvage_parses_bed_bath_from_unit_type(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import agents.runners.emit_comp_finder_envelope as helper

    monkeypatch.setattr(helper, "REPO_ROOT", tmp_path)

    deal_root = tmp_path / "soho_lakewood"
    run_id = "run_001"
    intake = deal_root / "outputs" / run_id / "intake"
    intake.mkdir(parents=True, exist_ok=True)
    intake.joinpath("canonical_deal.json").write_text(
        json.dumps(
            {
                "metadata": {
                    "deal_id": "Soho Lakewood",
                    "address": "7610 Skillman St, Dallas, TX 75231",
                    "market": "dallas_tx",
                    "as_of_date": "2026-05-21",
                },
                "unit_cohorts": [
                    {
                        "cohort_id": "30217a2",
                        "unit_type": "30217a2",
                        "unit_count": 30,
                        "sqft": 687,
                        "bedrooms": 1,
                        "bathrooms": 1.0,
                    }
                ],
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    config_path = tmp_path / "agents" / "configs" / "dallas_tx_soho_lakewood.yaml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        yaml.safe_dump(
            {
                "notes": {
                    "subject_property": {
                        "name": "Soho Lakewood",
                        "address": "7610 Skillman St, Dallas, TX 75231",
                    }
                },
                "comp_monitoring": {
                    "subjects": [
                        {
                            "floorplan_summary_csv": "reports/dallas-tx/soho-lakewood/rent-roll/clean/floorplan_summary.csv"
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )

    property_dir = tmp_path / "reports" / "dallas-tx" / "soho-lakewood"
    tracking = property_dir / "comps" / "tracking"
    tracking.mkdir(parents=True, exist_ok=True)
    (property_dir / "rent-roll" / "clean").mkdir(parents=True, exist_ok=True)
    (property_dir / "rent-roll" / "clean" / "floorplan_summary.csv").write_text("x\n", encoding="utf-8")
    (tracking / "property_database.csv").write_text(
        "\n".join(
            [
                "property,address,distance_mi,units,year_built,stories,management_company,notes",
                "Soho Lakewood,7610 Skillman St,0,64,1986,3,Cirrus,Subject property",
                "The Kendrick,7324 Skillman Street,0.31,405,1984,3,S2 Residential,Direct-site comp",
            ]
        ),
        encoding="utf-8",
    )
    (tracking / "rent_history.csv").write_text(
        "\n".join(
            [
                "date,property,unit_type,sq_ft,face_rent,rent_psf,units_available,source",
                "2026-05-21,The Kendrick,1BR/1BA,677,900,1.33,,Playwright direct-site",
            ]
        ),
        encoding="utf-8",
    )
    (tracking / "concession_history.csv").write_text(
        "date,property,concession_type,value,terms,source\n",
        encoding="utf-8",
    )

    response = build_comp_finder_response(
        deal_root=deal_root,
        deal_slug="soho_lakewood",
        run_id=run_id,
    )

    grouped = json.loads(
        (deal_root / "outputs" / run_id / "market_study" / "comps_cohort_grouped.json").read_text(
            encoding="utf-8"
        )
    )
    entries = grouped["comps_by_cohort"]["1BR_1.0BA_687sf"]
    assert response.payload is not None
    assert entries[0]["property_name"] == "The Kendrick"
    assert entries[0]["bedrooms"] == 1
    assert entries[0]["bathrooms"] == 1.0


def test_build_comp_finder_response_normalizes_display_market_labels_for_config_lookup(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import agents.runners.emit_comp_finder_envelope as helper

    monkeypatch.setattr(helper, "REPO_ROOT", tmp_path)

    deal_root = tmp_path / "state_at_fishers"
    run_id = "run_001"
    intake = deal_root / "outputs" / run_id / "intake"
    intake.mkdir(parents=True, exist_ok=True)
    intake.joinpath("canonical_deal.json").write_text(
        json.dumps(
            {
                "metadata": {
                    "deal_id": "State at Fishers",
                    "address": "10510 Kings Way Road, Fishers, IN",
                    "market": "Fishers, IN",
                    "as_of_date": "2026-06-01",
                },
                "unit_cohorts": [
                    {
                        "cohort_id": "studio",
                        "unit_count": 10,
                        "sqft": 626,
                        "bedrooms": 0,
                        "bathrooms": 1.0,
                    },
                    {
                        "cohort_id": "one_br",
                        "unit_count": 20,
                        "sqft": 742,
                        "bedrooms": 1,
                        "bathrooms": 1.0,
                    },
                ],
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    cfg = {
        "notes": {
            "subject_property": {
                "name": "State at Fishers",
                "address": "10510 Kings Way Road, Fishers, IN",
            }
        },
        "comp_monitoring": {
            "subjects": [
                {
                    "floorplan_summary_csv": "reports/fishers-in/state-at-fishers/rent-roll/clean/floorplan_summary.csv"
                }
            ]
        },
    }
    config_path = tmp_path / "agents" / "configs" / "fishers_in_state_at_fishers.yaml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(yaml.safe_dump(cfg), encoding="utf-8")

    property_dir = tmp_path / "reports" / "fishers-in" / "state-at-fishers"
    tracking = property_dir / "comps" / "tracking"
    tracking.mkdir(parents=True, exist_ok=True)
    (property_dir / "rent-roll" / "clean").mkdir(parents=True, exist_ok=True)
    (property_dir / "rent-roll" / "clean" / "floorplan_summary.csv").write_text("x\n", encoding="utf-8")
    (tracking / "property_database.csv").write_text(
        "\n".join(
            [
                "property,address,distance_mi,units,year_built,stories",
                "State at Fishers,10510 Kings Way Road,0.0,261,2024,4",
                "Comp A,Fishers IN,1.2,220,2014,3",
                "Comp B,Fishers IN,1.6,242,2014,4",
                "Comp C,Fishers IN,2.0,381,2025,4",
            ]
        ),
        encoding="utf-8",
    )
    (tracking / "rent_history.csv").write_text(
        "\n".join(
            [
                "date,property,address,unit_type,beds,baths,sq_ft,face_rent,effective_rent,rent_psf,units_available",
                "2026-05-08,Comp A,Fishers IN,1BR,1,1,742,$1504,$1504,$2.03,3",
                "2026-05-08,Comp A,Fishers IN,Studio,0,1,700,$1389,$1389,$1.98,1",
                "2026-05-08,Comp B,Fishers IN,1BR,1,1,748,$1350,$1350,$1.81,3",
                "2026-05-08,Comp B,Fishers IN,2BR,2,2,1191,$1790,$1790,$1.50,1",
                "2026-05-08,Comp C,Fishers IN,1BR,1,1,757,$1254,$1254,$1.66,3",
                "2026-05-08,Comp C,Fishers IN,2BR,2,2,1176,$1831,$1831,$1.56,3",
            ]
        ),
        encoding="utf-8",
    )
    (tracking / "concession_history.csv").write_text(
        "\n".join(
            [
                "date,property,address,unit_type,face_rent,concession_type,concession_value,free_months,effective_rent,lease_requirement",
                "2026-05-08,Comp A,Fishers IN,All,$1504,None,0,0,$1504,12",
                "2026-05-08,Comp B,Fishers IN,All,$1350,None,0,0,$1350,12",
                "2026-05-08,Comp C,Fishers IN,All,$1254,None,0,0,$1254,12",
            ]
        ),
        encoding="utf-8",
    )

    response = build_comp_finder_response(
        deal_root=deal_root,
        deal_slug="state_at_fishers",
        run_id=run_id,
    )

    assert response.status == "needs_analyst_input"
    assert response.payload is not None
    assert response.payload["comps_relative"] == f"outputs/{run_id}/comps/comps.json"
    comps_payload = json.loads((deal_root / "outputs" / run_id / "comps" / "comps.json").read_text())
    assert comps_payload["subject"]["metro_slug"] == "fishers_in"
    assert comps_payload["subject"]["metro_display"] == "Fishers, IN"
