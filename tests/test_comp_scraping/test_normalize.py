from __future__ import annotations

from etl.comp_scraping.normalize import (
    merge_partials_default,
    normalize_shape,
    summarize_units_by_signature,
)


def test_summarize_units_by_signature_buckets_by_signature() -> None:
    units = [
        {"beds": 1, "baths": 1, "sqft": 700, "rent": 1500, "floorplan_name": "A1"},
        {"beds": 1, "baths": 1, "sqft": 700, "rent": 1550, "floorplan_name": "A1"},
        {"beds": 2, "baths": 2, "sqft": 1100, "rent": 2000, "floorplan_name": "B1"},
    ]
    out = summarize_units_by_signature(units)
    assert len(out) == 2
    a1 = next(p for p in out if p["beds"] == 1)
    assert a1["available_units"] == 2
    assert a1["rent_min"] == 1500
    assert a1["rent_max"] == 1550
    assert a1["floorplan_name"] == "A1"
    b1 = next(p for p in out if p["beds"] == 2)
    assert b1["available_units"] == 1
    assert b1["rent_min"] == 2000


def test_normalize_shape_a_payload_to_shape_b() -> None:
    shape_a = {
        "available_units": [
            {"beds": 1, "baths": 1, "sqft": 700, "rent": 1500, "floorplan_name": "A1"},
        ],
        "floorplan_summary": [],
        "specials": ["1 month free"],
        "parser": "entrata",
    }
    out = normalize_shape(shape_a)
    assert out["platform"] == "entrata"
    assert len(out["floorplans"]) == 1
    assert out["floorplans"][0]["beds"] == 1
    assert out["specials"] == ["1 month free"]


def test_normalize_passes_through_shape_b() -> None:
    shape_b = {
        "platform": "cortland",
        "floorplans": [
            {
                "floorplan_name": "A1",
                "beds": 1,
                "baths": 1.0,
                "sqft": 700,
                "rent_min": 1500,
                "rent_max": 1500,
                "available_units": 3,
            }
        ],
        "units": [],
        "specials": [],
    }
    out = normalize_shape(shape_b)
    assert out["platform"] == "cortland"
    assert len(out["floorplans"]) == 1
    assert out["floorplans"][0]["available_units"] == 3


def test_normalize_unknown_payload_returns_empty() -> None:
    out = normalize_shape({"random": "data"})
    assert out["floorplans"] == []
    assert out["units"] == []


def test_merge_partials_dedups_floorplans_by_name() -> None:
    a1_a = {"floorplan_name": "A1", "beds": 1, "rent_min": 1500}
    a1_b = {"floorplan_name": "A1", "beds": 1, "rent_min": 1600}
    b1 = {"floorplan_name": "B1", "beds": 2, "rent_min": 2000}
    partials = [
        {"floorplans": [a1_a], "specials": ["1 month free"]},
        {"floorplans": [a1_b], "specials": ["1 month free"]},
        {"floorplans": [b1], "specials": ["8 weeks free"]},
    ]
    merged = merge_partials_default("test", partials)
    assert len(merged["floorplans"]) == 2
    assert {p["floorplan_name"] for p in merged["floorplans"]} == {"A1", "B1"}
    # Lower (better) rent_min wins; the existing entry's slot is preserved.
    a1 = next(p for p in merged["floorplans"] if p["floorplan_name"] == "A1")
    assert a1["rent_min"] == 1500
    assert sorted(merged["specials"]) == ["1 month free", "8 weeks free"]


def test_merge_partials_preserves_richer_data_across_pages() -> None:
    """Multi-page capture: when a later partial has more units or wider rent
    range for the same plan, those richer values must win — not first-seen."""

    page1 = {
        "floorplans": [
            {
                "floorplan_name": "A1",
                "beds": 1,
                "baths": 1.0,
                "sqft": 700,
                "rent_min": 1600,
                "rent_max": 1700,
                "available_units": 3,
            }
        ]
    }
    page2 = {
        "floorplans": [
            {
                "floorplan_name": "A1",
                "beds": 1,
                "baths": 1.0,
                "sqft": 700,
                "rent_min": 1500,  # better starting rent
                "rent_max": 1800,  # wider top
                "available_units": 7,  # richer count
            }
        ]
    }
    merged = merge_partials_default("test", [page1, page2])
    assert len(merged["floorplans"]) == 1
    plan = merged["floorplans"][0]
    assert plan["available_units"] == 7
    assert plan["rent_min"] == 1500
    assert plan["rent_max"] == 1800


def test_merge_partials_keeps_same_name_different_sqft_separate() -> None:
    """Several plans sharing a name (e.g. multiple "Studio" plans at
    different square footages) must not collapse into one row. Regression
    test for the William's 5-studio layout."""

    plans = [
        {"floorplan_name": "Studio", "beds": 0, "baths": 1.0, "sqft": 459, "rent_min": 1150},
        {"floorplan_name": "Studio", "beds": 0, "baths": 1.0, "sqft": 518, "rent_min": 1595},
        {"floorplan_name": "Studio", "beds": 0, "baths": 1.0, "sqft": 619, "rent_min": 1545},
        {"floorplan_name": "Studio", "beds": 0, "baths": 1.0, "sqft": 688, "rent_min": 1720},
        {"floorplan_name": "Studio", "beds": 0, "baths": 1.0, "sqft": 793, "rent_min": 1980},
    ]
    merged = merge_partials_default("test", [{"floorplans": plans}])
    assert len(merged["floorplans"]) == 5
    sqfts = sorted(p["sqft"] for p in merged["floorplans"])
    assert sqfts == [459, 518, 619, 688, 793]


def test_merge_partials_backfills_missing_scalars() -> None:
    """Backfill rent on a same-name same-shape plan reported across pages."""

    page1 = {
        "floorplans": [
            {"floorplan_name": "A1", "beds": 1, "baths": 1.0, "sqft": 720,
             "rent_min": 1500, "rent_max": 1500},
        ]
    }
    page2 = {
        "floorplans": [
            {"floorplan_name": "A1", "beds": 1, "baths": 1.0, "sqft": 720,
             "rent_min": 0, "rent_max": 1700, "available_units": 4},
        ]
    }
    merged = merge_partials_default("test", [page1, page2])
    assert len(merged["floorplans"]) == 1
    plan = merged["floorplans"][0]
    assert plan["rent_min"] == 1500
    assert plan["rent_max"] == 1700
    assert plan["available_units"] == 4
