from __future__ import annotations

from pathlib import Path

import yaml

from etl.collect_comps_snapshot import parse_entrata_floorplans
from etl.comp_scraping import (
    BUILTIN_ENGINES,
    LLM_FALLBACK_ENGINE,
    collect_comp,
    resolve_engine,
)
from etl.comp_scraping.engines._base import SimpleEngine
import pytest


def _stub_fetch(text_by_url: dict[str, str]):  # noqa: ANN202
    """Build a fetch_fn stub that returns the text mapped to each URL."""

    def fetch(url: str, method: str, timeout: int) -> dict:
        if url in text_by_url:
            return {"ok": True, "status": 200, "text": text_by_url[url], "error": None}
        return {"ok": False, "status": 404, "text": "", "error": "not found"}

    return fetch


def test_resolve_engine_picks_url_substring_match() -> None:
    eng = resolve_engine(
        "https://cortland.com/apartments/cortland-vesta/floorplans/",
        "",
        BUILTIN_ENGINES,
        fallback=LLM_FALLBACK_ENGINE,
    )
    assert eng.name == "cortland"


def test_resolve_engine_picks_html_fingerprint_when_no_url_match() -> None:
    eng = resolve_engine(
        "https://example.com/floorplans/",
        '<script>{"MinRent":1500,"MaxRent":1600}</script>',
        BUILTIN_ENGINES,
        fallback=LLM_FALLBACK_ENGINE,
    )
    assert eng.name == "entrata"


def test_rentcafe_wins_over_entrata_when_both_fingerprints_present() -> None:
    """RentCafe pages sometimes carry capitalised ``MinRent`` JSON keys
    inherited from shared widgets. The resolver must prefer the more-specific
    RentCafe match in that case."""

    html = (
        '<html><head>... rentcafe ...</head><body>'
        '<script>{"floorplans":[{"id":1,"beds":1,"sqft":700}]}</script>'
        '<script>{"MinRent":1500,"FloorplanId":3,"AvailableCount":2}</script>'
        "</body></html>"
    )
    eng = resolve_engine(
        "https://www.example.com/floorplans/",
        html,
        BUILTIN_ENGINES,
        fallback=LLM_FALLBACK_ENGINE,
    )
    assert eng.name == "rentcafe"


def test_resolve_engine_falls_back_to_llm_engine() -> None:
    eng = resolve_engine(
        "https://example.com/",
        "<html><body>Just some text</body></html>",
        BUILTIN_ENGINES,
        fallback=LLM_FALLBACK_ENGINE,
    )
    assert eng.name == "llm_fallback"


def test_resolve_engine_recognizes_known_vanity_domains() -> None:
    assert (
        resolve_engine(
            "https://www.saratogaridgeaustin.com/austin/saratoga-ridge/conventional/",
            "",
            BUILTIN_ENGINES,
            fallback=LLM_FALLBACK_ENGINE,
        ).name
        == "entrata"
    )
    assert (
        resolve_engine(
            "https://www.logansmillliving.com/floorplans",
            "",
            BUILTIN_ENGINES,
            fallback=LLM_FALLBACK_ENGINE,
        ).name
        == "rentcafe"
    )


def test_collect_comp_records_quality_gate_meta() -> None:
    floorplans_html = (
        '<script>'
        '{"id":1,"apartment_number":"A101","floorplan":3,"floorplan_name":"A1",'
        '"bedrooms":"1","rent_min":1500,"rent_max":1600,"square_feet":700}'
        ',{"id":2,"apartment_number":"A102","floorplan":3,"floorplan_name":"A1",'
        '"bedrooms":"1","rent_min":1550,"rent_max":1650,"square_feet":700}'
        ',{"id":3,"apartment_number":"B201","floorplan":4,"floorplan_name":"B1",'
        '"bedrooms":"2","rent_min":2000,"rent_max":2100,"square_feet":1100}'
        '</script>'
    )
    fetch_fn = _stub_fetch(
        {
            "https://cortland.com/apartments/test-prop": floorplans_html,
            "https://cortland.com/apartments/test-prop/floorplans/": floorplans_html,
            "https://cortland.com/apartments/test-prop/": floorplans_html,
            "https://cortland.com/apartments/test-prop/apply/": "",
        }
    )

    result = collect_comp(
        base_url="https://cortland.com/apartments/test-prop",
        units_total=20,
        fetch_fn=fetch_fn,
        registry=BUILTIN_ENGINES,
        fallback=LLM_FALLBACK_ENGINE,
    )

    assert result["platform"] == "cortland"
    assert len(result["floorplans"]) == 2
    meta = result["scrape_meta"]
    assert meta["engine"] == "cortland"
    assert meta["passes_gate"] is True
    assert meta["min_threshold"] == 1
    assert "floorplans" in meta["pages_fetched"]


def test_collect_comp_stops_probing_after_floorplans_fingerprint_and_reuses_html() -> None:
    base_url = "https://example.com"
    floorplans_url = f"{base_url}/floorplans/"
    calls: list[tuple[str, str, int]] = []
    engine = SimpleEngine(
        name="probe_engine",
        url_substrings=[],
        html_fingerprints=["probe-engine"],
        parser=lambda _: {
            "platform": "probe_engine",
            "floorplans": [
                {
                    "floorplan_name": "A1",
                    "beds": 1,
                    "baths": 1,
                    "sqft": 700,
                    "rent_min": 1000,
                    "rent_max": 1000,
                    "available_units": 1,
                }
            ],
            "units": [],
            "specials": [],
        },
    )

    def fetch(url: str, method: str, timeout: int) -> dict:
        calls.append((url, method, timeout))
        text = "probe-engine" if url == floorplans_url else "homepage"
        return {"ok": True, "status": 200, "text": text, "error": None}

    result = collect_comp(
        base_url=base_url,
        units_total=1,
        fetch_fn=fetch,
        registry=[engine],
    )

    assert result["platform"] == "probe_engine"
    assert calls == [
        (base_url, "auto", 30),
        (floorplans_url, "auto", 30),
    ]


def test_parse_entrata_floorplans_handles_grid_card_markup() -> None:
    html = """
    <ul id="floorplans-1" class="fp-grid-list">
      <li class="fp-grid-item">
        <div class="grid-details">
          <h4 class="fp-name show-available">
            <a class="fp-name-link" href="https://example.com/floorplans/11a-1230028-1/" title="View More Information">1+1A</a>
          </h4>
          <span class="available-units">3 Available</span>
          <div class="details-col bed-bath">
            <span class="title">Bed / Bath</span>
            <span class="value">1 <span class="small-abbr">bd</span> / 1 <span class="small-abbr">ba</span></span>
          </div>
          <div class="details-col rent">
            <span class="title">Rent</span>
            <div class="value fee-transparency-wrapper">
              <span class="fee-transparency-text"> From $929/month </span>
            </div>
          </div>
          <div class="details-col sq-feet">
            <span class="title">Sq. Ft</span>
            <span class="value">650</span>
          </div>
        </div>
        <li class="grid-link action">
          <button type="button" class="btn availability" data-url="https://www.saratogaridgeaustin.com/?module=check_availability&is_secure=1&property[id]=100126316&action=view_unit_spaces&property_floorplan[id]=1230028&cached_rate_available=1&min_rent=$929&max_rent=$1,345&min_advertised_base_rent=929&max_advertised_base_rent=1345"></button>
        </li>
      </li>
    </ul>
    """

    payload = parse_entrata_floorplans(html)

    assert payload["parser"] == "entrata"
    assert payload["available_units"] == [
        {
            "unit_number": None,
            "beds": 1,
            "baths": 1.0,
            "sqft": 650.0,
            "rent": 929.0,
            "rent_max": 1345.0,
            "available_date": None,
            "available_count": 3,
            "apply_url": None,
            "floorplan_id": "1230028",
            "floorplan_name": "1+1A",
        }
    ]


def test_parse_entrata_floorplans_skips_malformed_array_before_valid_floorplans() -> None:
    html = """
    <html><body>
      <script>
        window.renderedTracking = [
          {"Name": "Template", "Beds": 1, "Baths": 1, "MinSqFt": 650, "MinRent": 999,}
        ];
      </script>
      <section>Rendered card copy with bracketed text [not JSON]</section>
      <script>
        window.__entrataFloorplans = [
          {
            "Name": "A1",
            "Beds": 1,
            "Baths": 1,
            "MinSqFt": 700,
            "MinRent": 1450,
            "MaxRent": 1510,
            "AvailableCount": 2,
            "FloorplanId": "fp-a1"
          },
          {
            "Name": "B1",
            "Beds": 2,
            "Baths": 2,
            "MinSqFt": 1050,
            "MinRent": 1895,
            "MaxRent": 1995,
            "AvailableCount": 1,
            "FloorplanId": "fp-b1"
          }
        ];
      </script>
    </body></html>
    """

    payload = parse_entrata_floorplans(html)

    assert payload["parser"] == "entrata"
    assert payload["available_units"] == [
        {
            "unit_number": None,
            "beds": 1,
            "baths": 1.0,
            "sqft": 700.0,
            "rent": 1450.0,
            "rent_max": 1510.0,
            "available_date": None,
            "available_count": 2,
            "apply_url": None,
            "floorplan_id": "fp-a1",
            "floorplan_name": "A1",
        },
        {
            "unit_number": None,
            "beds": 2,
            "baths": 2.0,
            "sqft": 1050.0,
            "rent": 1895.0,
            "rent_max": 1995.0,
            "available_date": None,
            "available_count": 1,
            "apply_url": None,
            "floorplan_id": "fp-b1",
            "floorplan_name": "B1",
        },
    ]

def test_fetch_pages_does_not_reuse_static_html_for_playwright_page() -> None:
    from etl.comp_scraping.dispatch import fetch_pages
    from etl.comp_scraping.types import PageSpec

    calls: list[tuple[str, str, int]] = []

    def fetch(url: str, method: str, timeout: int) -> dict:
        calls.append((url, method, timeout))
        return {"ok": True, "status": 200, "text": "rendered", "error": None}

    pages = fetch_pages(
        "https://www.maac.com/texas/dallas/maa-highlands-north",
        [
            PageSpec(
                name="property_page",
                url_template="{base}/",
                fetch_method="playwright",
                parser="floorplans",
                required=True,
            )
        ],
        fetch,
        base_html="static shell",
    )

    assert calls == [
        (
            "https://www.maac.com/texas/dallas/maa-highlands-north/",
            "playwright",
            30,
        )
    ]
    assert pages[0][1]["text"] == "rendered"


def test_collect_comp_uses_yottareal_floorplans_api_when_dbaid_present() -> None:
    api_url = "https://residentapis.yottareal.com/api/DBA/GetFloorPlans/34"
    api_json = (
        '{"unitTypeModel":[{"dbaUnitTypeId":12,"dbaUnitType":"A1","numBedRooms":1,'
        '"bathRooms":1,"area":650,"availableUnitsCount":1}],'
        '"hotSheetUnitsModel":[{"dbaUnitTypeId":12,"unitNumber":"101",'
        '"squareFeet":650,"rent":1450,"dateAvailable":"2026-06-12"}]}'
    )

    calls: list[tuple[str, str, int]] = []

    def fetch(url: str, method: str, timeout: int) -> dict:
        calls.append((url, method, timeout))
        if url == api_url:
            return {"ok": True, "status": 200, "text": api_json, "error": None}
        return {"ok": False, "status": 404, "text": "", "error": "not found"}

    result = collect_comp(
        base_url="https://adaraportal.yottareal.com/dba/floorplans?dbaid=34",
        units_total=1,
        fetch_fn=fetch,
        registry=BUILTIN_ENGINES,
        fallback=LLM_FALLBACK_ENGINE,
    )

    assert result["platform"] == "yottareal"
    assert result["floorplans"][0]["floorplan_name"] == "A1"
    assert result["units"][0]["unit_number"] == "101"
    assert result["units"][0]["rent"] == 1450
    assert api_url in {call[0] for call in calls}


def test_example_metro_sunrise_bluffs_points_to_yottareal_floorplans_url() -> None:
    pytest.skip(reason="requires a real-property config that is not shipped in the public tree")
    cfg = yaml.safe_load(
        Path("agents/configs/austin_tx_example_metro.yaml").read_text(encoding="utf-8")
    )
    sunrise = next(
        comp
        for comp in (cfg.get("comp_monitoring") or {}).get("comps") or []
        if comp.get("name") == "Sunrise Bluffs"
    )
    assert sunrise["property_url"] == "https://adaraportal.yottareal.com/dba/floorplans?dbaid=34"
    assert sunrise["direct_floorplans_url"] == "https://adaraportal.yottareal.com/dba/floorplans?dbaid=34"
