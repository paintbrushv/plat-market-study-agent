"""Comp-property scraping package.

Public API:

* :data:`BUILTIN_ENGINES` — list of all known leasing-engine modules.
* :data:`LLM_FALLBACK_ENGINE` — synthetic engine for unrecognised sites.
* :func:`collect_comp` — top-level per-comp orchestrator.
* :func:`resolve_engine` — engine resolver (URL → fingerprint → fallback).
* :mod:`quality_gate` — minimum-floorplan threshold checks.
"""

from __future__ import annotations

from etl.comp_scraping import quality_gate
from etl.comp_scraping.dispatch import (
    collect_comp,
    fetch_pages,
    resolve_engine,
    run_engine,
)
from etl.comp_scraping.engines import BUILTIN_ENGINES
from etl.comp_scraping.llm_engine import LLM_FALLBACK_ENGINE
from etl.comp_scraping.normalize import (
    merge_partials_default,
    normalize_shape,
    summarize_units_by_signature,
)
from etl.comp_scraping.types import (
    EngineModule,
    PageSpec,
    PartialResult,
    ScrapeResult,
)

__all__ = [
    "BUILTIN_ENGINES",
    "LLM_FALLBACK_ENGINE",
    "EngineModule",
    "PageSpec",
    "PartialResult",
    "ScrapeResult",
    "collect_comp",
    "fetch_pages",
    "merge_partials_default",
    "normalize_shape",
    "quality_gate",
    "resolve_engine",
    "run_engine",
    "summarize_units_by_signature",
]
