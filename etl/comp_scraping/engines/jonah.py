"""Hy.ly / Jonah Digital leasing engine.

Hy.ly properties expose their full inventory via a SightMap XHR
(``sightmap.com/app/api/v1/<asset>/sightmaps/<id>``) — the DOM widget paginates
to ~24 "Available Now" units only. The legacy collector intercepts the XHR via
:func:`etl.collect_comps_snapshot.fetch_sightmap_via_playwright` for these. We
preserve that behaviour here: the engine declares a ``playwright_xhr`` page,
the dispatcher routes to the XHR fetcher, and the SightMap JSON parser
produces the full unit list.

For metros where the XHR isn't reachable, the legacy DOM-based
``parse_jonah_floorplans`` is used as a soft fallback by also declaring the
``floorplans`` page.
"""

from __future__ import annotations

from typing import Any

from etl.collect_comps_snapshot import (
    parse_jonah_floorplans,
    parse_sightmap_json,
)
from etl.comp_scraping.engines._base import SimpleEngine, _parse_specials_banner
from etl.comp_scraping.normalize import merge_partials_default, normalize_shape
from etl.comp_scraping.types import PageSpec, PartialResult, ScrapeResult


class _JonahEngine(SimpleEngine):
    def parse_page(self, spec: PageSpec, content: Any) -> PartialResult:  # noqa: ANN401
        if spec.parser == "sightmap_xhr":
            payload = content
            if not isinstance(payload, dict):
                return PartialResult(page_name=spec.name)
            try:
                parsed = parse_sightmap_json(payload)
            except ValueError:
                return PartialResult(page_name=spec.name)
            normalised = normalize_shape(parsed)
            return PartialResult(
                page_name=spec.name,
                floorplans=list(normalised.get("floorplans") or []),
                units=list(normalised.get("units") or []),
                specials=list(normalised.get("specials") or []),
                raw_meta={"parser": "sightmap"},
            )
        if spec.parser == "specials_banner":
            return _parse_specials_banner(spec.name, content if isinstance(content, str) else "")
        text = content if isinstance(content, str) else ""
        try:
            payload = parse_jonah_floorplans(text)
        except ValueError:
            return PartialResult(page_name=spec.name)
        normalised = normalize_shape(payload)
        return PartialResult(
            page_name=spec.name,
            floorplans=list(normalised.get("floorplans") or []),
            units=list(normalised.get("units") or []),
            specials=list(normalised.get("specials") or []),
            raw_meta={"parser": "jonah"},
        )

    def merge(self, partials: list[PartialResult]) -> ScrapeResult:
        # SightMap XHR returns the complete inventory; the DOM widget only
        # paginates to ~24 "Available Now" units. When the XHR succeeded,
        # discard DOM partials so their lower counts don't dilute the merge.
        xhr_succeeded = any(
            (p.raw_meta or {}).get("parser") == "sightmap" and p.floorplans
            for p in partials
        )
        keep = [
            p for p in partials
            if not (xhr_succeeded and p.page_name == "floorplans_dom")
        ]
        merged = merge_partials_default(
            self.name,
            [
                {"floorplans": p.floorplans, "units": p.units, "specials": p.specials}
                for p in keep
            ],
        )
        return ScrapeResult(
            platform=merged["platform"],
            floorplans=merged["floorplans"],
            units=merged["units"],
            specials=merged["specials"],
            confidence=self.confidence,
        )


JONAH_ENGINE = _JonahEngine(
    name="jonah_sightmap",
    url_substrings=["thejamesonhighland.com", "my.hy.ly", "hyly.us"],
    # ``sightmap.com/embed`` covers properties (e.g. Fitzroy, 20 Midtown,
    # The 600) that iframe SightMap into a custom property site — the
    # iframe's XHR still bubbles up to the parent page's response listener,
    # so the existing XHR interceptor handles them transparently.
    html_fingerprints=[
        "jd-fp-unit-card",
        "my.hy.ly",
        "sightmap.com/app/api",
        "sightmap.com/embed",
    ],
    pages=[
        PageSpec(
            name="sightmap_xhr",
            url_template="{base}/floorplans/",
            fetch_method="playwright_xhr",
            parser="sightmap_xhr",
            required=False,
        ),
        PageSpec(
            name="floorplans_dom",
            url_template="{base}/floorplans/",
            fetch_method="playwright",
            parser="floorplans",
        ),
        PageSpec(
            name="home_specials",
            url_template="{base}/",
            fetch_method="auto",
            parser="specials_banner",
        ),
    ],
)
