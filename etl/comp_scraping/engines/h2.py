"""H2 Real Estate listings engine."""

from __future__ import annotations

from etl.collect_comps_snapshot import parse_h2_realestate_listings
from etl.comp_scraping.engines._base import SimpleEngine
from etl.comp_scraping.types import PageSpec

H2_ENGINE = SimpleEngine(
    name="h2",
    url_substrings=["h2realestate", "h2-realestate"],
    html_fingerprints=["/ MONTH"],
    parser=parse_h2_realestate_listings,
    pages=[
        PageSpec(
            name="listings",
            url_template="{base}/",
            fetch_method="auto",
            parser="floorplans",
            required=True,
        ),
    ],
)
