import asyncio
import json
from pathlib import Path

import pytest

from agents.sdk import tools as _tools_module
from agents.sdk.tools import (
    pull_comps_tool,
    normalize_rent_roll_tool,
    generate_section_tool,
)


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# pull_comps_tool — production wiring
# ---------------------------------------------------------------------------


def test_pull_comps_tool_returns_rows_with_source_attribution(monkeypatch, tmp_path):
    """Smoke test against a synthetic snapshot dir — the tool returns a JSON
    payload with row_count + sources keys. (The private repo pointed this at a
    live comps snapshot; that data is not shipped, so we provide a synthetic
    snapshot loader result instead.)"""
    fake_rows = [
        {
            "name": "Foo Apartments",
            "address": "1 Foo St",
            "source": "snapshot:synthetic",
        },
        {
            "name": "Bar Lofts",
            "address": "2 Bar Ave",
            "source": "snapshot:synthetic",
        },
    ]

    def fake_loader(metro: str, asset_class: str, limit: int):
        return fake_rows[:limit]

    monkeypatch.setattr(_tools_module, "load_comps_snapshot", fake_loader)

    result = _run(
        pull_comps_tool.handler({"metro": "austin", "asset_class": "multifamily", "limit": 5})
    )
    text = result["content"][0]["text"]
    payload = json.loads(text)
    assert "row_count" in payload
    assert "sources" in payload
    assert isinstance(payload["sources"], list) and payload["sources"]


def test_pull_comps_tool_loads_real_snapshot(monkeypatch):
    """Production wiring: pull_comps_tool must dispatch to the snapshot loader and
    return rows tagged with a source string for governance attribution."""
    fake_rows = [
        {"name": "Foo Apartments", "address": "1 Foo St", "source": "snapshot:austin_tx_example_metro"},
        {"name": "Bar Lofts", "address": "2 Bar Ave", "source": "snapshot:austin_tx_example_metro"},
    ]

    def fake_loader(metro: str, asset_class: str, limit: int):
        assert metro == "austin"
        assert asset_class == "multifamily"
        assert limit == 5
        return fake_rows

    monkeypatch.setattr(_tools_module, "load_comps_snapshot", fake_loader)

    result = _run(
        pull_comps_tool.handler({"metro": "austin", "asset_class": "multifamily", "limit": 5})
    )
    text = result["content"][0]["text"]
    payload = json.loads(text)
    assert payload["row_count"] == 2
    assert payload["sources"] == ["snapshot:austin_tx_example_metro"]
    assert payload["rows"] == fake_rows
    assert result.get("is_error") is not True


def test_pull_comps_returns_json_payload_with_structured_rows(monkeypatch):
    """Contract: pull_comps_tool returns a JSON-decodable payload whose 'rows'
    array contains the full per-comp dicts (not a summary string). This
    eliminates the need for the agent to fabricate dicts to pass into
    generate_section."""
    fake_rows = [
        {"name": f"Comp{i}", "address": f"{i} Main",
         "source": "snapshot:austin_tx_example_metro:2026-04-20"}
        for i in range(3)
    ]
    monkeypatch.setattr(_tools_module, "load_comps_snapshot", lambda *a, **kw: fake_rows)

    result = _run(
        pull_comps_tool.handler({"metro": "austin", "asset_class": "multifamily", "limit": 5})
    )
    payload = json.loads(result["content"][0]["text"])
    assert payload["row_count"] == 3
    assert isinstance(payload["rows"], list) and len(payload["rows"]) == 3
    for r in payload["rows"]:
        assert "source" in r
    assert payload["sources"] == ["snapshot:austin_tx_example_metro:2026-04-20"]


def test_pull_comps_tool_fails_when_no_snapshot(monkeypatch):
    """If the loader returns no rows, the tool must fail-loud (governance)."""
    monkeypatch.setattr(_tools_module, "load_comps_snapshot", lambda *a, **kw: [])

    result = _run(
        pull_comps_tool.handler({"metro": "no_such_metro", "asset_class": "multifamily", "limit": 5})
    )
    assert result.get("is_error") is True
    assert "FAIL" in result["content"][0]["text"]


def test_pull_comps_tool_fails_when_row_missing_source(monkeypatch):
    """Every comp must have a source. A row without source is a governance failure."""
    monkeypatch.setattr(
        _tools_module,
        "load_comps_snapshot",
        lambda *a, **kw: [{"name": "X", "source": "snapshot:foo"}, {"name": "Y"}],
    )
    result = _run(
        pull_comps_tool.handler({"metro": "austin", "asset_class": "multifamily", "limit": 5})
    )
    assert result.get("is_error") is True


# ---------------------------------------------------------------------------
# normalize_rent_roll_tool — production wiring
# ---------------------------------------------------------------------------


def test_normalize_rent_roll_tool_handles_empty():
    result = _run(normalize_rent_roll_tool.handler({"rent_roll_csv": ""}))
    assert result.get("is_error") is True


def test_normalize_rent_roll_tool_dispatches_to_standardizer(monkeypatch):
    """Tool should write CSV to a temp file, detect config, run transform_rent_roll,
    and report row counts. We monkeypatch the underlying repo function."""
    captured: dict = {}

    def fake_normalize(csv_text: str):
        captured["csv"] = csv_text
        return [
            {"unit_id": "101", "floorplan_code": "A1", "bed_type": "1BR", "sqft": 700, "market_rent": 1200.0},
            {"unit_id": "102", "floorplan_code": "A1", "bed_type": "1BR", "sqft": 700, "market_rent": 1250.0},
        ]

    monkeypatch.setattr(_tools_module, "normalize_csv_string", fake_normalize)

    csv = "Unit,Floor Plan,Beds,Sq Ft,Target Rent\n101,A1,1,700,1200\n102,A1,1,700,1250\n"
    result = _run(normalize_rent_roll_tool.handler({"rent_roll_csv": csv}))
    assert captured["csv"] == csv
    text = result["content"][0]["text"]
    assert "records=2" in text
    assert result.get("is_error") is not True


def test_normalize_rent_roll_tool_fails_loud_on_unparseable(monkeypatch):
    """If the standardizer can't recognize the format and yields zero rows, fail loud."""
    monkeypatch.setattr(_tools_module, "normalize_csv_string", lambda csv: [])
    result = _run(
        normalize_rent_roll_tool.handler({"rent_roll_csv": "garbage,column,headers\n1,2,3\n"})
    )
    assert result.get("is_error") is True


# ---------------------------------------------------------------------------
# generate_section_tool — production wiring
# ---------------------------------------------------------------------------


def test_generate_section_tool_requires_evidence():
    result = _run(
        generate_section_tool.handler({"section": "Comp Set Overview", "evidence": []})
    )
    assert result.get("is_error") is True


def test_generate_section_tool_renders_comp_set_overview_markdown():
    """For 'Comp Set Overview', the tool must produce a markdown table that
    cites every evidence row's source — no hallucination, no placeholder."""
    evidence = [
        {
            "name": "Stassney at SoCo",
            "address": "1800 E Stassney Ln, Austin, TX 78744",
            "units_available": 4,
            "min_rent": 953,
            "max_rent": 1091,
            "source": "snapshot:austin_tx_example_metro:2026-04-20",
        },
        {
            "name": "Foxwood",
            "address": "6503 Bluff Springs Rd, Austin, TX 78744",
            "units_available": 2,
            "min_rent": 999,
            "max_rent": 1150,
            "source": "snapshot:austin_tx_example_metro:2026-04-20",
        },
    ]
    result = _run(
        generate_section_tool.handler({"section": "Comp Set Overview", "evidence": evidence})
    )
    assert result.get("is_error") is not True
    text = result["content"][0]["text"]
    assert "Comp Set Overview" in text
    assert "Stassney at SoCo" in text
    assert "Foxwood" in text
    # Every row's source must appear (governance: no uncited content)
    assert "snapshot:austin_tx_example_metro:2026-04-20" in text


def test_generate_section_tool_unknown_section_still_grounded():
    """For sections without a dedicated renderer, the tool must still produce a
    grounded summary (not fabricate) — emit evidence count + sources."""
    evidence = [{"name": "X", "source": "snapshot:foo"}]
    result = _run(
        generate_section_tool.handler({"section": "Unknown Section", "evidence": evidence})
    )
    assert result.get("is_error") is not True
    text = result["content"][0]["text"]
    assert "Unknown Section" in text
    assert "snapshot:foo" in text


def test_render_comp_set_overview_handles_json_encoded_evidence():
    """If the agent passes the JSON payload from pull_comps as a single string
    entry, the renderer must decode it and render N rows (not 1 placeholder)."""
    rows = [
        {"name": "Stassney at SoCo", "address": "1800 E Stassney",
         "units_available": 4, "min_rent": 953, "max_rent": 1091,
         "source": "snapshot:austin_tx_example_metro:2026-04-20"},
        {"name": "Foxwood", "address": "6503 Bluff Springs",
         "units_available": 2, "min_rent": 999, "max_rent": 1150,
         "source": "snapshot:austin_tx_example_metro:2026-04-20"},
        {"name": "Mesh", "address": "9 Mesh Way",
         "units_available": 1, "min_rent": 1200, "max_rent": 1200,
         "source": "snapshot:austin_tx_example_metro:2026-04-20"},
    ]
    payload = json.dumps({"row_count": 3, "rows": rows,
                          "sources": ["snapshot:austin_tx_example_metro:2026-04-20"]})

    result = _run(
        generate_section_tool.handler({"section": "Comp Set Overview", "evidence": [payload]})
    )
    assert result.get("is_error") is not True
    text = result["content"][0]["text"]
    # All three property names must appear (renderer decoded the JSON)
    assert "Stassney at SoCo" in text
    assert "Foxwood" in text
    assert "Mesh" in text
    # Must have 3 numbered table rows (not 1)
    assert "| 1 |" in text
    assert "| 2 |" in text
    assert "| 3 |" in text


def test_render_comp_set_overview_handles_json_encoded_list():
    """Renderer also accepts a JSON list (not just a {rows: [...]} envelope)."""
    rows = [
        {"name": "A", "address": "1", "source": "snapshot:foo"},
        {"name": "B", "address": "2", "source": "snapshot:foo"},
    ]
    result = _run(
        generate_section_tool.handler(
            {"section": "Comp Set Overview", "evidence": [json.dumps(rows)]}
        )
    )
    assert result.get("is_error") is not True
    text = result["content"][0]["text"]
    assert "| A |" in text
    assert "| B |" in text


# ---------------------------------------------------------------------------
# load_comps_snapshot — discovery helper
# ---------------------------------------------------------------------------


def test_load_comps_snapshot_reads_latest_for_metro(tmp_path, monkeypatch):
    """The snapshot loader picks the most recent JSON for the metro and emits
    rows with source=snapshot:<metro_slug>:<date>."""
    snap_dir = tmp_path / "data" / "public" / "processed" / "comps"
    snap_dir.mkdir(parents=True)

    older = {
        "run_date": "2026-01-01",
        "metro": "Austin, TX",
        "metro_slug": "austin_tx_example_metro",
        "comps": [{"name": "Old Place", "address": "Old"}],
    }
    newer = {
        "run_date": "2026-04-20",
        "metro": "Austin, TX",
        "metro_slug": "austin_tx_example_metro",
        "comps": [
            {"name": "Stassney at SoCo", "address": "1800 E Stassney"},
            {"name": "Foxwood", "address": "6503 Bluff Springs"},
        ],
    }
    (snap_dir / "2026-01-01_austin_tx_example_metro_comps_snapshot.json").write_text(json.dumps(older))
    (snap_dir / "2026-04-20_austin_tx_example_metro_comps_snapshot.json").write_text(json.dumps(newer))

    monkeypatch.setattr(_tools_module, "_COMPS_DIR", snap_dir)

    rows = _tools_module.load_comps_snapshot(metro="austin", asset_class="multifamily", limit=5)
    assert len(rows) == 2
    assert rows[0]["name"] == "Stassney at SoCo"
    for r in rows:
        assert r["source"] == "snapshot:austin_tx_example_metro:2026-04-20"


def test_load_comps_snapshot_respects_limit(tmp_path, monkeypatch):
    snap_dir = tmp_path / "snaps"
    snap_dir.mkdir()
    payload = {
        "run_date": "2026-04-20",
        "metro": "Austin, TX",
        "metro_slug": "austin_tx",
        "comps": [{"name": f"Comp{i}", "address": f"{i} Main"} for i in range(10)],
    }
    (snap_dir / "2026-04-20_austin_tx_comps_snapshot.json").write_text(json.dumps(payload))
    monkeypatch.setattr(_tools_module, "_COMPS_DIR", snap_dir)

    rows = _tools_module.load_comps_snapshot(metro="austin", asset_class="multifamily", limit=3)
    assert len(rows) == 3


def test_load_comps_snapshot_returns_empty_when_no_match(tmp_path, monkeypatch):
    snap_dir = tmp_path / "snaps"
    snap_dir.mkdir()
    monkeypatch.setattr(_tools_module, "_COMPS_DIR", snap_dir)
    rows = _tools_module.load_comps_snapshot(metro="nowhere", asset_class="x", limit=5)
    assert rows == []


def test_load_comps_snapshot_metro_match_is_token_not_substring(tmp_path, monkeypatch):
    """metro='dallas' must match slugs whose first token is 'dallas',
    and metro='dall' must NOT match any (no substring leak)."""
    snap_dir = tmp_path / "snaps"
    snap_dir.mkdir()

    a = {"run_date": "2026-04-01", "metro": "Dallas, TX", "metro_slug": "dallas_xyz",
         "comps": [{"name": "A", "address": "1 A"}]}
    b = {"run_date": "2026-04-15", "metro": "Dallas, TX",
         "metro_slug": "dallas_far_north_example",
         "comps": [{"name": "B", "address": "2 B"}]}
    (snap_dir / "2026-04-01_dallas_xyz_comps_snapshot.json").write_text(json.dumps(a))
    (snap_dir / "2026-04-15_dallas_far_north_example_comps_snapshot.json").write_text(json.dumps(b))
    monkeypatch.setattr(_tools_module, "_COMPS_DIR", snap_dir)

    rows = _tools_module.load_comps_snapshot(metro="dallas", asset_class="x", limit=5)
    # Latest wins (dallas_far_north_example, 2026-04-15)
    assert len(rows) == 1
    assert rows[0]["name"] == "B"

    # Substring 'dall' must not match 'dallas_*'
    rows_substring = _tools_module.load_comps_snapshot(metro="dall", asset_class="x", limit=5)
    assert rows_substring == []


def test_load_comps_snapshot_does_not_cross_leak(tmp_path, monkeypatch):
    """metro='dallas' must NOT pick up austin_*.json files, even if some
    other slug coincidentally contains 'dallas' as a non-leading substring."""
    snap_dir = tmp_path / "snaps"
    snap_dir.mkdir()
    austin1 = {"run_date": "2026-04-20", "metro": "Austin, TX",
               "metro_slug": "austin_example_metro",
               "comps": [{"name": "AustinA", "address": "1"}]}
    austin2 = {"run_date": "2026-04-21", "metro": "Austin, TX",
               "metro_slug": "austin_other",
               "comps": [{"name": "AustinB", "address": "2"}]}
    dallas = {"run_date": "2026-04-22", "metro": "Dallas, TX",
              "metro_slug": "dallas_xyz",
              "comps": [{"name": "DallasA", "address": "3"}]}
    (snap_dir / "2026-04-20_austin_example_metro_comps_snapshot.json").write_text(json.dumps(austin1))
    (snap_dir / "2026-04-21_austin_other_comps_snapshot.json").write_text(json.dumps(austin2))
    (snap_dir / "2026-04-22_dallas_xyz_comps_snapshot.json").write_text(json.dumps(dallas))
    monkeypatch.setattr(_tools_module, "_COMPS_DIR", snap_dir)

    rows = _tools_module.load_comps_snapshot(metro="dallas", asset_class="x", limit=5)
    assert len(rows) == 1
    assert rows[0]["name"] == "DallasA"
    # No austin row may leak in
    for r in rows:
        assert "Austin" not in r["name"]


# ---------------------------------------------------------------------------
# normalize_csv_string — discovery helper
# ---------------------------------------------------------------------------


def test_normalize_csv_string_handles_appfolio_csv():
    csv = (
        "Unit,Floor Plan,Beds,Baths,Sq Ft,Target Rent,Tenant Name,Move-In Date,Lease End Date\n"
        "101,A1,1,1,700,1200,John Doe,01/15/2025,12/31/2025\n"
        "102,A1,1,1,700,1250,Jane Roe,03/01/2025,02/28/2026\n"
    )
    rows = _tools_module.normalize_csv_string(csv)
    assert len(rows) == 2
    assert rows[0]["unit_id"] == "101"
    assert rows[0]["floorplan_code"] == "A1"
