"""Swifty (Apartmentalist) leasing engine."""

from __future__ import annotations

from etl.collect_comps_snapshot import parse_swifty_floorplans
from etl.comp_scraping.engines._base import SimpleEngine
from etl.comp_scraping.types import PageSpec

SWIFTY_ENGINE = SimpleEngine(
    name="swifty",
    url_substrings=["swiftyapp.io", "swifty.com"],
    # ``single-floorplan`` is too generic — collides with WordPress-wrapped
    # RentCafe pages that emit a same-named CSS class. ``swifty-app`` is the
    # vendor-specific marker.
    html_fingerprints=["swifty-app"],
    parser=parse_swifty_floorplans,
    pages=[
        PageSpec(
            name="floorplans",
            url_template="{base}/floorplans/",
            fetch_method="auto",
            parser="floorplans",
            required=True,
        ),
        PageSpec(
            name="home_specials",
            url_template="{base}/",
            fetch_method="auto",
            parser="specials_banner",
        ),
    ],
)
