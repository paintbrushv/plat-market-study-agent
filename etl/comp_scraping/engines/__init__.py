"""Built-in engine registry.

Each engine module declares ``url_substrings`` / ``html_fingerprints`` for
detection and uses :class:`SimpleEngine` to wrap an existing legacy parser
function. Add a new engine by creating a sibling file and appending to
``BUILTIN_ENGINES`` below.
"""

from __future__ import annotations

from etl.comp_scraping.engines.appfolio_listings import APPFOLIO_LISTINGS_ENGINE
from etl.comp_scraping.engines.cortland import CORTLAND_ENGINE
from etl.comp_scraping.engines.entrata import ENTRATA_ENGINE
from etl.comp_scraping.engines.h2 import H2_ENGINE
from etl.comp_scraping.engines.jonah import JONAH_ENGINE
from etl.comp_scraping.engines.livebh import LIVEBH_ENGINE
from etl.comp_scraping.engines.maa import MAA_ENGINE
from etl.comp_scraping.engines.rentcafe import RENTCAFE_ENGINE
from etl.comp_scraping.engines.rentvision import RENTVISION_ENGINE
from etl.comp_scraping.engines.resi_rendered import RESI_RENDERED_ENGINE
from etl.comp_scraping.engines.static_floorplans import STATIC_FLOORPLANS_ENGINE
from etl.comp_scraping.engines.swifty import SWIFTY_ENGINE
from etl.comp_scraping.engines.tonti_rentcafe import TONTI_RENTCAFE_ENGINE
from etl.comp_scraping.engines.yottareal import YOTTAREAL_ENGINE
from etl.comp_scraping.types import EngineModule

# Order matters for ambiguous URLs / overlapping HTML fingerprints. Place
# vendor-specific engines (Cortland, Jonah/SightMap, LiveBH, Yottareal,
# RentVision) before generic platforms (RentCafe, Entrata) — RentCafe before
# Entrata because it's the more common platform and its fingerprint substrings
# can co-occur in pages that ship Entrata-flavoured ``"MinRent"`` JSON keys for
# some shared widgets.
BUILTIN_ENGINES: list[EngineModule] = [
    CORTLAND_ENGINE,
    JONAH_ENGINE,
    APPFOLIO_LISTINGS_ENGINE,
    RESI_RENDERED_ENGINE,
    SWIFTY_ENGINE,
    H2_ENGINE,
    LIVEBH_ENGINE,
    MAA_ENGINE,
    YOTTAREAL_ENGINE,
    RENTVISION_ENGINE,
    TONTI_RENTCAFE_ENGINE,
    STATIC_FLOORPLANS_ENGINE,
    RENTCAFE_ENGINE,
    ENTRATA_ENGINE,
]

__all__ = ["BUILTIN_ENGINES"]
