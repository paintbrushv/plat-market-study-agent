"""LLM fallback engine — Tier 3 of the dispatch cascade.

Activates when no static engine matches a comp's URL/HTML or when the matched
engine fails the quality gate. Fetches the floorplans page (Playwright if
available) and any specials banner, hands the rendered HTML to
:func:`etl.comp_scraping.llm_extract.extract_with_llm`, and returns Shape-B.
"""

from __future__ import annotations

from typing import Any

from etl.comp_scraping import llm_extract
from etl.comp_scraping.engines._base import _parse_specials_banner
from etl.comp_scraping.normalize import merge_partials_default
from etl.comp_scraping.types import (
    EngineModule,
    PageSpec,
    PartialResult,
    ScrapeResult,
)


class LlmFallbackEngine:
    """Synthetic engine — never matches by URL/fingerprint; used only as the
    explicit ``fallback`` argument to :func:`resolve_engine`.
    """

    name = "llm_fallback"
    url_substrings: list[str] = []
    html_fingerprints: list[str] = []

    def discover_pages(self, base_url: str) -> list[PageSpec]:
        return [
            PageSpec(
                name="floorplans_llm",
                url_template="{base}/floorplans/",
                fetch_method="playwright",
                parser="llm",
                required=True,
            ),
            PageSpec(
                name="home_llm",
                url_template="{base}/",
                fetch_method="auto",
                parser="specials_banner",
            ),
        ]

    def parse_page(self, spec: PageSpec, content: Any) -> PartialResult:  # noqa: ANN401
        if spec.parser == "specials_banner":
            return _parse_specials_banner(spec.name, content if isinstance(content, str) else "")
        if spec.parser != "llm":
            return PartialResult(page_name=spec.name)

        text = content if isinstance(content, str) else ""
        if not text or not llm_extract.is_available():
            return PartialResult(page_name=spec.name)

        try:
            payload = llm_extract.extract_with_llm(text)
        except Exception as exc:
            return PartialResult(
                page_name=spec.name,
                raw_meta={"llm_error": str(exc)[:200]},
            )

        return PartialResult(
            page_name=spec.name,
            floorplans=list(payload.get("floorplans") or []),
            units=[],
            specials=list(payload.get("specials") or []),
            raw_meta={
                "llm_confidence": payload.get("confidence"),
                "llm_notes": payload.get("notes"),
            },
        )

    def merge(self, partials: list[PartialResult]) -> ScrapeResult:
        merged = merge_partials_default(
            self.name,
            [
                {
                    "floorplans": p.floorplans,
                    "units": p.units,
                    "specials": p.specials,
                }
                for p in partials
            ],
        )
        confidences = [p.raw_meta.get("llm_confidence") for p in partials if p.raw_meta]
        confidence = "low"
        for c in confidences:
            if c == "high":
                confidence = "high"
                break
            if c == "medium" and confidence != "high":
                confidence = "medium"
        return ScrapeResult(
            platform=merged["platform"],
            floorplans=merged["floorplans"],
            units=merged["units"],
            specials=merged["specials"],
            confidence=confidence,
        )


LLM_FALLBACK_ENGINE: EngineModule = LlmFallbackEngine()
