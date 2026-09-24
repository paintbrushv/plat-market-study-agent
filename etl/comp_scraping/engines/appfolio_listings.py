"""AppFolio listings engine (structured listings page).

This engine handles AppFolio's listings page — a richer endpoint than the
generic property site. The legacy ``parse_appfolio_listings_page`` is the
parser; the YAML field ``appfolio_listings_url`` continues to take priority
in the dispatcher (handled by the wrapper in ``collect_comps_snapshot.py``).
"""

from __future__ import annotations

from etl.collect_comps_snapshot import parse_appfolio_listings_page
from etl.comp_scraping.engines._base import SimpleEngine
from etl.comp_scraping.types import PageSpec

APPFOLIO_LISTINGS_ENGINE = SimpleEngine(
    name="appfolio_listings",
    url_substrings=["appfolio.com/listings", "/listings?filters", "appfoliopm"],
    html_fingerprints=["Appfolio.Listing", "listing-item"],
    parser=parse_appfolio_listings_page,
    pages=[
        PageSpec(
            name="listings",
            url_template="{base}",
            fetch_method="auto",
            parser="floorplans",
            required=True,
        ),
    ],
)
