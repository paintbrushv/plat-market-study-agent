from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

SCHEMA_PATH = Path("prompts/metrics_schema.json")


def load_schema() -> dict[str, Any]:
    data = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    return cast(dict[str, Any], data)


def test_schema_requires_metro_fundamentals() -> None:
    schema = load_schema()
    assert "metro_fundamentals" in schema["required"]


def test_factual_status_enum() -> None:
    schema = load_schema()
    status_property = schema["properties"]["metro_fundamentals"]["items"]["properties"]["status"]
    status_enum = status_property["enum"]
    assert sorted(status_enum) == ["Factual", "Inferred"]


def test_forecasts_require_horizons_sources_and_confidence() -> None:
    schema = load_schema()
    forecasts = schema["properties"]["forecasts"]
    assert forecasts["minItems"] >= 3

    forecast_required = forecasts["items"]["required"]
    assert set(forecast_required) == {"horizon", "metric", "forecast", "source", "confidence"}


def test_forecast_horizons_cover_one_three_five_years() -> None:
    schema = load_schema()
    forecast_horizons = schema["properties"]["forecasts"]["items"]["properties"]["horizon"]["enum"]
    assert set(forecast_horizons) == {"1-year", "3-year", "5-year"}

    contains_clauses = schema["properties"]["forecasts"].get("allOf", [])
    const_horizons = {
        clause["contains"]["properties"]["horizon"]["const"]
        for clause in contains_clauses
        if "contains" in clause
    }
    assert const_horizons == {"1-year", "3-year", "5-year"}
