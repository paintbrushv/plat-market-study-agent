from __future__ import annotations

from etl.collect_comps_snapshot import _is_low_confidence, build_report


def _make_snapshot(comps: list[dict]) -> dict:
    return {"comps": comps}


def _make_comp(name: str, confidence: str = "high", passes_gate: bool = True, engine: str = "rentcafe") -> dict:
    return {
        "name": name,
        "address": "123 Test St",
        "direct_floorplans_url": None,
        "apartments_com_url": None,
        "direct": {
            "platform": engine,
            "floorplans": [],
            "units": [],
            "specials": [],
            "scrape_meta": {
                "confidence": confidence,
                "passes_gate": passes_gate,
                "engine": engine,
            },
        },
        "apartments_com": {},
        "errors": [],
        "needs_parser_or_manual_review": False,
    }


# ---------------------------------------------------------------------------
# _is_low_confidence helper tests
# ---------------------------------------------------------------------------

def test_is_low_confidence_returns_true_for_confidence_low() -> None:
    comp = _make_comp("Test Prop", confidence="low", passes_gate=True)
    assert _is_low_confidence(comp) is True


def test_is_low_confidence_returns_true_for_passes_gate_false() -> None:
    comp = _make_comp("Test Prop", confidence="high", passes_gate=False)
    assert _is_low_confidence(comp) is True


def test_is_low_confidence_returns_true_for_llm_engine() -> None:
    comp = _make_comp("Test Prop", confidence="high", passes_gate=True, engine="llm_fallback")
    assert _is_low_confidence(comp) is True


def test_is_low_confidence_returns_false_for_high_confidence() -> None:
    comp = _make_comp("Test Prop", confidence="high", passes_gate=True, engine="rentcafe")
    assert _is_low_confidence(comp) is False


def test_is_low_confidence_true_for_salvage_payload_without_scrape_meta() -> None:
    """Salvage paths (e.g. parse_static_property_floorplans, yottareal API)
    publish direct payloads that carry no scrape_meta at all — they never
    passed the quality gate and must be treated as low confidence."""
    comp = _make_comp("Salvaged Prop")
    del comp["direct"]["scrape_meta"]
    assert _is_low_confidence(comp) is True


def test_is_low_confidence_true_for_missing_passes_gate() -> None:
    """scrape_meta present but passes_gate absent — align with the loose
    infra-flag predicate (`not meta.get("passes_gate")`)."""
    comp = _make_comp("No Gate Prop")
    del comp["direct"]["scrape_meta"]["passes_gate"]
    assert _is_low_confidence(comp) is True


def test_is_low_confidence_true_for_none_passes_gate() -> None:
    comp = _make_comp("None Gate Prop")
    comp["direct"]["scrape_meta"]["passes_gate"] = None
    assert _is_low_confidence(comp) is True


def test_is_low_confidence_false_when_no_direct_payload() -> None:
    """A comp with no direct payload publishes no floorplan table at all;
    its fetch errors are already surfaced separately in the report."""
    comp = _make_comp("Empty Prop")
    comp["direct"] = {}
    assert _is_low_confidence(comp) is False
    comp.pop("direct")
    assert _is_low_confidence(comp) is False


# ---------------------------------------------------------------------------
# build_report preamble injection tests
# ---------------------------------------------------------------------------

def test_build_report_includes_preamble_for_low_confidence_comp() -> None:
    low_comp = _make_comp("Sunset Lofts", confidence="low", passes_gate=False)
    snapshot = _make_snapshot([low_comp])

    report = build_report(
        run_date="2026-07-02",
        metro="Austin, TX",
        metro_slug="austin_tx",
        subjects=[],
        snapshot=snapshot,
    )

    assert "> **Data Quality Notice:**" in report
    assert "Sunset Lofts" in report
    assert "1 property" in report
    assert "LLM-estimated or was collected" in report
    assert "treat as low confidence" in report
    assert "Verify before use." in report


def test_build_report_includes_preamble_for_multiple_low_confidence_comps() -> None:
    comp1 = _make_comp("Alpha", confidence="low", passes_gate=False)
    comp2 = _make_comp("Beta", confidence="high", passes_gate=False)
    comp3 = _make_comp("Gamma", confidence="high", passes_gate=True, engine="llm_chain")
    snapshot = _make_snapshot([comp1, comp2, comp3])

    report = build_report(
        run_date="2026-07-02",
        metro="Austin, TX",
        metro_slug="austin_tx",
        subjects=[],
        snapshot=snapshot,
    )

    assert "> **Data Quality Notice:**" in report
    assert "3 properties" in report
    assert "Alpha" in report
    assert "Beta" in report
    assert "Gamma" in report


def test_build_report_omits_preamble_when_all_comps_high_confidence() -> None:
    comp1 = _make_comp("Alpha", confidence="high", passes_gate=True, engine="rentcafe")
    comp2 = _make_comp("Beta", confidence="high", passes_gate=True, engine="apartments_com")
    snapshot = _make_snapshot([comp1, comp2])

    report = build_report(
        run_date="2026-07-02",
        metro="Austin, TX",
        metro_slug="austin_tx",
        subjects=[],
        snapshot=snapshot,
    )

    assert "> **Data Quality Notice:**" not in report
    assert "LLM-estimated" not in report


def test_build_report_includes_preamble_for_salvage_payload_without_meta() -> None:
    """A comp published via a scrape_meta-less salvage path must carry the notice."""
    comp = _make_comp("Jubilee Studios")
    del comp["direct"]["scrape_meta"]
    comp["direct"]["floorplans"] = [
        {
            "floorplan_name": "Studio",
            "beds": 0,
            "baths": 1.0,
            "sqft": 0.0,
            "rent_min": 899.0,
            "rent_max": 899.0,
            "available_units": 0,
        }
    ]
    snapshot = _make_snapshot([comp])

    report = build_report(
        run_date="2026-07-02",
        metro="Austin, TX",
        metro_slug="austin_tx",
        subjects=[],
        snapshot=snapshot,
    )

    assert "> **Data Quality Notice:**" in report
    assert "Jubilee Studios" in report


def test_build_report_omits_preamble_for_empty_snapshot() -> None:
    snapshot = _make_snapshot([])

    report = build_report(
        run_date="2026-07-02",
        metro="Austin, TX",
        metro_slug="austin_tx",
        subjects=[],
        snapshot=snapshot,
    )

    assert "> **Data Quality Notice:**" not in report
