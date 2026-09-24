"""Shared types for comp scraping engines.

The package implements a registry-based dispatch over property-website leasing
engines (Entrata, RentCafe, SightMap, Cortland, AppFolio, Resi, etc.). Each
engine declares the pages it knows about and how to parse them; the dispatcher
detects the engine from a URL or rendered HTML, fetches the declared pages,
parses each, and merges the partials into a normalised ``ScrapeResult``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

# Fetch method understood by ``etl.http_client.fetch_html`` / ``safe_fetch``.
FetchMethod = Literal["auto", "curl", "playwright", "playwright_xhr"]

# Confidence tag set by the engine merge step.
Confidence = Literal["high", "medium", "low"]


@dataclass(frozen=True)
class PageSpec:
    """One page an engine wants fetched and parsed.

    ``url_template`` is interpolated against the comp's ``base_url`` (the
    leasing-site root) using ``str.format(base=...)``. Use a literal absolute
    URL when the template depends on something other than ``base``.
    """

    name: str
    url_template: str
    fetch_method: FetchMethod = "auto"
    parser: str = "parse_floorplans"
    required: bool = False
    per_plan: bool = False


@dataclass
class PartialResult:
    """Output of parsing a single page. Engines produce one per ``PageSpec``."""

    page_name: str
    floorplans: list[dict[str, Any]] = field(default_factory=list)
    units: list[dict[str, Any]] = field(default_factory=list)
    specials: list[str] = field(default_factory=list)
    raw_meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class ScrapeResult:
    """Merged, normalised output of a full multi-page scrape for one comp.

    Always emitted in Shape B (``floorplans`` / ``units`` / ``specials`` /
    ``platform``). Downstream readers (``build_report``) read
    ``direct.get("floorplans")`` for table rendering.
    """

    platform: str
    floorplans: list[dict[str, Any]] = field(default_factory=list)
    units: list[dict[str, Any]] = field(default_factory=list)
    specials: list[str] = field(default_factory=list)
    confidence: Confidence = "high"
    pages_fetched: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


class EngineModule(Protocol):
    """Protocol every engine module must satisfy."""

    name: str
    url_substrings: list[str]
    html_fingerprints: list[str]

    def discover_pages(self, base_url: str) -> list[PageSpec]: ...

    def parse_page(self, spec: PageSpec, content: Any) -> PartialResult: ...  # noqa: ANN401

    def merge(self, partials: list[PartialResult]) -> ScrapeResult: ...


# Function injected by the dispatcher so engines don't import http_client
# directly (keeps engine modules pure / testable).
FetchFn = Callable[[str, FetchMethod, int], dict[str, Any]]
