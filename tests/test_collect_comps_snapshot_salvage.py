from __future__ import annotations

from etl.collect_comps_snapshot import (
    CompConfig,
    _site_specific_salvage,
    _yottareal_api_url,
)


def test_yottareal_api_url_is_derived_from_floorplans_page_url() -> None:
    assert (
        _yottareal_api_url("https://adaraportal.yottareal.com/dba/floorplans?dbaid=34")
        == "https://residentapis.yottareal.com/api/DBA/GetFloorPlans/34"
    )


def test_site_specific_salvage_uses_yottareal_api_payload(monkeypatch) -> None:
    comp = CompConfig(
        name="Sunrise Bluffs",
        address="1704 Nelms Dr, Austin, TX 78744",
        direct_floorplans_url="https://adaraportal.yottareal.com/dba/floorplans?dbaid=34",
        direct_availability_url=None,
        apartments_com_url=None,
        appfolio_listings_url=None,
        property_url="https://adaraportal.yottareal.com/dba/floorplans?dbaid=34",
    )

    def fake_safe_fetch(url: str, timeout_s: int = 30, method: str = "auto") -> dict:
        assert url == "https://residentapis.yottareal.com/api/DBA/GetFloorPlans/34"
        assert method == "auto"
        return {
            "ok": True,
            "status": 200,
            "text": (
                '{"unitTypeModel":[{"dbaUnitTypeId":12,"dbaUnitType":"A1",'
                '"numBedRooms":1,"bathRooms":1,"area":650,"availableUnitsCount":1}],'
                '"hotSheetUnitsModel":[{"dbaUnitTypeId":12,"unitNumber":"101",'
                '"squareFeet":650,"rent":1450,"dateAvailable":"2026-06-12"}]}'
            ),
            "error": None,
            "as_of_utc": "2026-06-09T00:00:00Z",
        }

    monkeypatch.setattr("etl.collect_comps_snapshot._safe_fetch", fake_safe_fetch)

    payload = _site_specific_salvage(
        comp=comp,
        effective_direct_url=comp.direct_floorplans_url or "",
        base_for_engine=comp.property_url or "",
        units_total=1,
        run_date="2026-06-09",
        metro_slug="austin_tx_example_metro",
    )

    assert payload is not None
    assert payload["platform"] == "yottareal"
    assert payload["floorplans"][0]["floorplan_name"] == "A1"
    assert payload["units"][0]["unit_number"] == "101"
    assert payload["units"][0]["rent"] == 1450


def test_site_specific_salvage_can_rerun_browser_backed_engine(monkeypatch) -> None:
    comp = CompConfig(
        name="Logans Mill",
        address="1912 E William Cannon Dr, Austin, TX 78744",
        direct_floorplans_url="https://www.logansmillliving.com/floorplans",
        direct_availability_url=None,
        apartments_com_url=None,
        appfolio_listings_url=None,
        property_url=None,
    )

    calls: list[tuple[str, int | None, str | None]] = []

    def fake_collect_comp(**kwargs):  # noqa: ANN003
        calls.append(
            (
                kwargs["base_url"],
                kwargs["units_total"],
                kwargs["comp_name"],
            )
        )
        return {
            "platform": "rentcafe",
            "floorplans": [
                {
                    "floorplan_name": "A1A",
                    "beds": 1,
                    "baths": 1.0,
                    "sqft": 650,
                    "rent_min": 1425,
                    "rent_max": 1425,
                    "available_units": 3,
                }
            ],
            "units": [
                {
                    "unit_number": "101",
                    "beds": 1,
                    "baths": 1.0,
                    "sqft": 650,
                    "rent": 1425,
                    "rent_max": 1425,
                    "available_date": None,
                    "available_count": 1,
                    "apply_url": None,
                    "floorplan_id": None,
                    "floorplan_name": "A1A",
                }
            ],
            "specials": [],
        }

    monkeypatch.setattr("etl.comp_scraping.collect_comp", fake_collect_comp)

    payload = _site_specific_salvage(
        comp=comp,
        effective_direct_url=comp.direct_floorplans_url or "",
        base_for_engine="https://www.logansmillliving.com",
        units_total=1,
        run_date="2026-06-09",
        metro_slug="austin_tx_example_metro",
    )

    assert payload is not None
    assert payload["platform"] == "rentcafe"
    assert payload["floorplans"][0]["floorplan_name"] == "A1A"
    assert calls == [("https://www.logansmillliving.com", 1, "Logans Mill")]
