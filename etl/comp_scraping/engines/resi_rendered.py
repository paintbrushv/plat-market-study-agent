"""Resi (rendered card) engine — generic UIKit cards on Vue/Resi sites."""

from __future__ import annotations

from typing import Any

from etl.collect_comps_snapshot import parse_resi_rendered_cards, parse_sightmap_json
from etl.comp_scraping.engines._base import SimpleEngine
from etl.comp_scraping.normalize import normalize_shape
from etl.comp_scraping.types import PageSpec, PartialResult


class _ResiRenderedEngine(SimpleEngine):
    def parse_page(self, spec: PageSpec, content: Any) -> PartialResult:  # noqa: ANN401
        if spec.parser == "sightmap_inventory":
            if not isinstance(content, dict):
                return PartialResult(page_name=spec.name)
            try:
                payload = parse_sightmap_json(content)
            except ValueError:
                return PartialResult(page_name=spec.name)
            normalised = normalize_shape(payload)
            return PartialResult(
                page_name=spec.name,
                floorplans=list(normalised.get("floorplans") or []),
                units=list(normalised.get("units") or []),
                specials=list(normalised.get("specials") or []),
                raw_meta={"parser": payload.get("parser")},
            )
        return super().parse_page(spec, content)


RESI_RENDERED_ENGINE = _ResiRenderedEngine(
    name="resi_rendered",
    url_substrings=["myashwoodpark.com"],
    html_fingerprints=["uk-tile uk-padding-small"],
    parser=parse_resi_rendered_cards,
    pages=[
        PageSpec(
            name="floorplans",
            url_template="{base}/floor-plans/",
            fetch_method="playwright",
            parser="floorplans",
            required=True,
        ),
        PageSpec(
            name="sightmap_inventory",
            url_template="{base}/floor-plans/",
            fetch_method="playwright_xhr",
            parser="sightmap_inventory",
        ),
        PageSpec(
            name="home_specials",
            url_template="{base}/",
            fetch_method="auto",
            parser="specials_banner",
        ),
    ],
)
