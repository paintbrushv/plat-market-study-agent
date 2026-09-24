"""Shared engine helpers.

Most engines wrap a single legacy parser function and only differ in their
URL/fingerprint declarations and which extra pages they want to fetch. The
:class:`SimpleEngine` dataclass captures that pattern.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from etl.comp_scraping.normalize import (
    merge_partials_default,
    normalize_shape,
)
from etl.comp_scraping.types import (
    Confidence,
    PageSpec,
    PartialResult,
    ScrapeResult,
)


@dataclass
class SimpleEngine:
    """An engine that wraps one parser function.

    ``parser`` consumes raw page text (HTML or JSON-as-text) and returns a
    legacy Shape-A or Shape-B dict. ``normalize_shape`` handles either shape.

    Multi-page support: declare additional ``pages`` beyond the default
    floorplans page. Their parsed output is merged via
    :func:`merge_partials_default`.
    """

    name: str
    url_substrings: list[str]
    html_fingerprints: list[str] = field(default_factory=list)
    parser: Callable[[str], dict[str, Any]] = field(default=lambda _: {})
    pages: list[PageSpec] = field(default_factory=list)
    confidence: Confidence = "high"

    def discover_pages(self, base_url: str) -> list[PageSpec]:
        if self.pages:
            return list(self.pages)
        return [
            PageSpec(
                name="floorplans",
                url_template="{base}/floorplans/",
                fetch_method="auto",
                parser="floorplans",
                required=True,
            )
        ]

    def parse_page(self, spec: PageSpec, content: Any) -> PartialResult:  # noqa: ANN401
        text = content if isinstance(content, str) else ""
        if spec.parser == "specials_banner":
            return _parse_specials_banner(spec.name, text)
        try:
            payload = self.parser(text)
        except ValueError:
            return PartialResult(page_name=spec.name)
        normalised = normalize_shape(payload)
        return PartialResult(
            page_name=spec.name,
            floorplans=list(normalised.get("floorplans") or []),
            units=list(normalised.get("units") or []),
            specials=list(normalised.get("specials") or []),
            raw_meta={"parser": payload.get("parser") or payload.get("platform")},
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
        return ScrapeResult(
            platform=merged["platform"],
            floorplans=merged["floorplans"],
            units=merged["units"],
            specials=merged["specials"],
            confidence=self.confidence,
        )


def _parse_specials_banner(page_name: str, html_text: str) -> PartialResult:
    """Extract free-rent / concession banner text from a homepage or specials page.

    Conservative — looks for the same patterns the Entrata parser already uses
    plus a few common alternates (look-and-lease, $X off, signing bonus). Used
    by every engine's optional ``/`` (homepage) page so we capture banner
    concessions that don't live on the floorplans tab.
    """

    import re

    if not html_text:
        return PartialResult(page_name=page_name)

    patterns = (
        r"(\d+\s*weeks?\s*free[^<.\n]{0,160})",
        r"(\d+\s*months?\s*free[^<.\n]{0,160})",
        r"(\$\d[\d,]*\s*off[^<.\n]{0,160})",
        r"(half\s*off[^<.\n]{0,160})",
        r"(look[\s-]?n[\s-]?lease[^<.\n]{0,160})",
        r"(look\s*and\s*lease[^<.\n]{0,160})",
        r"(zero\s*deposit[^<.\n]{0,120})",
        r"(no\s*application\s*fee[^<.\n]{0,120})",
        r"(signing\s*bonus[^<.\n]{0,160})",
    )
    found: list[str] = []
    for pat in patterns:
        for sm in re.finditer(pat, html_text, re.IGNORECASE):
            snippet = re.sub(r"\s+", " ", sm.group(1)).strip()
            snippet = re.sub(r"<[^>]+>", " ", snippet)
            snippet = re.sub(r"\s+", " ", snippet).strip()
            if snippet and snippet not in found:
                found.append(snippet)
            if len(found) >= 8:
                break
    return PartialResult(page_name=page_name, specials=found)
