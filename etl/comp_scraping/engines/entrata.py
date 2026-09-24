"""Entrata leasing engine.

Two layout families are supported:

1. **Floorplans-tab layout** — single ``/floorplans/`` page rendering all
   plans together (handled by :func:`parse_entrata_floorplans`).
2. **Prospect Portal layout** — homepage indexes per-floorplan detail pages
   at ``/floorplans/<city>-<state>/<property>/<plan-slug>/``. Each detail
   page exposes plan name + price + bed/bath/sqft. Handled by the
   :class:`_EntrataProspectPortalEngine` subclass below, which crawls the
   homepage, extracts detail URLs, and aggregates per-plan metrics.
"""

from __future__ import annotations

import re
from typing import Any

from etl.collect_comps_snapshot import parse_entrata_floorplans
from etl.comp_scraping.engines._base import SimpleEngine
from etl.comp_scraping.normalize import normalize_shape
from etl.comp_scraping.types import PageSpec, PartialResult


_PP_DETAIL_URL_RE = re.compile(
    r'https?://[^"\s]+/floorplans/[a-z]+-[A-Z]{2}/[a-z0-9-]+/[a-z0-9-]+-\d+-\d+/?',
    re.IGNORECASE,
)


def _to_float(text: str | None) -> float:
    if not text:
        return 0.0
    cleaned = re.sub(r"[^\d.]", "", str(text))
    return float(cleaned) if cleaned else 0.0


def _first_dollar(text: str | None) -> float:
    """Extract the first ``$X,XXX(.YY)?`` amount as a float."""

    if not text:
        return 0.0
    m = re.search(r"\$\s*([\d,]+(?:\.\d+)?)", str(text))
    if not m:
        return 0.0
    try:
        return float(m.group(1).replace(",", ""))
    except ValueError:
        return 0.0


def _beds_from_plan_name(name: str) -> int | None:
    """Heuristic bed-count derivation from the plan name prefix.

    Entrata Prospect Portal pages title themselves with a property-wide
    "Studio, 1-2 Bedroom..." string, so the global page title is unreliable.
    Most workforce property naming follows the convention ``A* = 1BR``,
    ``B* = 2BR``, ``C* = 3BR``, ``Efficiency``/``Studio`` = 0BR.
    """

    if not name:
        return None
    n = name.strip().upper()
    if n.startswith("EFFICIENCY") or n.startswith("STUDIO") or n.startswith("S"):
        if n.startswith("S") and not n.startswith("STU"):
            # ``S1`` could be a studio code; conservative fallthrough
            return None
        return 0
    if n.startswith("A"):
        return 1
    if n.startswith("B"):
        return 2
    if n.startswith("C"):
        return 3
    return None


def _parse_pp_detail(html: str) -> dict[str, Any] | None:
    """Parse one Entrata Prospect Portal floorplan detail page.

    Returns a unit-row dict (Shape-A row) or ``None`` if the page didn't
    render rent / beds. Looks for these markers:

    * ``<h1>`` or ``.fp-name`` → plan name (e.g. ``"A1"``)
    * ``.sticky-fp-info-wrapper`` text → ``"<plan> | $<rent>/month | ..."``
    * ``<title>`` → ``"<N> Bedroom <city> Apartments..."`` for bed extraction
    * Body text → ``"<sqft> SqFt"`` and ``"<n> Bath"``
    """

    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return None

    soup = BeautifulSoup(html, "html.parser")

    fp_name_el = soup.select_one(".fp-name") or soup.select_one("h1")
    name = fp_name_el.get_text(strip=True) if fp_name_el else ""
    if not name or len(name) > 24:  # h1 fallback can hit page title — guard
        return None

    rent = 0.0
    sticky = soup.select_one(".sticky-fp-info-wrapper")
    sticky_text = sticky.get_text(" ", strip=True) if sticky else ""
    m = re.search(r"\$\s*([\d,]+(?:\.\d+)?)\s*/\s*month", sticky_text)
    if m:
        rent = float(m.group(1).replace(",", ""))
    if rent <= 0:
        price_el = soup.select_one(".price-amount")
        if price_el:
            # ``.price-amount`` wraps both rent and lease term — only
            # extract the leading dollar amount, ignore "13mo" etc.
            rent = _first_dollar(price_el.get_text())
    if rent <= 0:
        return None

    body_text = soup.get_text(" ", strip=True)

    # Bed count: plan-name prefix is more reliable than the property-wide
    # ``<title>`` (which always reads "Studio, 1-2 Bedroom..."). Fall back
    # to body-text scanning only if the plan-name heuristic fails.
    beds_guess = _beds_from_plan_name(name)
    beds = beds_guess if beds_guess is not None else 0
    if beds_guess is None:
        m = re.search(r"(\d+)\s*Bedroom", body_text, re.IGNORECASE)
        if m:
            beds = int(m.group(1))
        elif re.search(r"\bstudio\b|\befficiency\b", name, re.IGNORECASE):
            beds = 0

    baths = 1.0
    m = re.search(r"(\d+(?:\.\d+)?)\s*Bath", body_text, re.IGNORECASE)
    if m:
        baths = float(m.group(1))

    sqft = 0.0
    m = re.search(r"([\d,]+)\s*SqFt", body_text, re.IGNORECASE) or re.search(
        r"([\d,]+)\s*sq\.?\s*ft", body_text, re.IGNORECASE
    )
    if m:
        sqft = _to_float(m.group(1))

    return {
        "unit_number": None,
        "beds": beds,
        "baths": baths,
        "sqft": sqft,
        "rent": rent,
        "rent_max": rent,
        "available_date": None,
        "available_count": 0,
        "apply_url": None,
        "floorplan_id": None,
        "floorplan_name": name,
    }


class _EntrataProspectPortalEngine(SimpleEngine):
    """Entrata variant whose homepage indexes per-floorplan detail pages.

    Detection: standard entrata fingerprints + at least 2 detail-page URLs
    matching the Prospect Portal pattern in the homepage HTML. When that
    structure is present, the engine parses each detail page individually
    rather than expecting a unified ``/floorplans/`` tab.
    """

    def discover_pages(self, base_url: str) -> list[PageSpec]:
        return [
            PageSpec(
                name="homepage",
                url_template=f"{base_url.rstrip('/')}/",
                fetch_method="auto",
                parser="floorplans",
                required=True,
            )
        ]

    def parse_page(self, spec: PageSpec, content: Any) -> PartialResult:  # noqa: ANN401
        text = content if isinstance(content, str) else ""

        # Try the standard floorplans-tab parser first; some Prospect Portal
        # sites still expose a unified JSON blob on the homepage.
        try:
            payload = parse_entrata_floorplans(text)
        except ValueError:
            payload = None

        if not payload or not payload.get("available_units"):
            # Fall through to the multi-page Prospect Portal crawl.
            payload = self._parse_prospect_portal(text)

        if not payload or not payload.get("available_units"):
            return PartialResult(page_name=spec.name)

        normalised = normalize_shape(payload)
        return PartialResult(
            page_name=spec.name,
            floorplans=list(normalised.get("floorplans") or []),
            units=list(normalised.get("units") or []),
            specials=list(normalised.get("specials") or []),
            raw_meta={"parser": payload.get("parser") or "entrata_pp"},
        )

    def _parse_prospect_portal(self, html: str) -> dict[str, Any] | None:
        from etl.http_client import fetch_html  # local import to avoid cycles

        urls = sorted(set(_PP_DETAIL_URL_RE.findall(html)))
        if len(urls) < 2:
            return None

        unit_rows: list[dict[str, Any]] = []
        for u in urls[:24]:  # safety cap
            status, page_html = fetch_html(u, method="auto", timeout_s=45)
            if status != 200 or not page_html:
                continue
            row = _parse_pp_detail(page_html)
            if row:
                unit_rows.append(row)

        if not unit_rows:
            return None

        return {
            "available_units": unit_rows,
            "floorplan_summary": [],
            "specials": [],
            "parser": "entrata_pp",
        }


# Standard floorplans-tab Entrata sites (entrata.com domains, /d2/ leasing
# pages, vanity-domain sites whose homepage exposes the inline JSON blob).
ENTRATA_ENGINE = _EntrataProspectPortalEngine(
    name="entrata",
    url_substrings=["entrata.com", ".entrata.", "/d2/", "saratogaridgeaustin.com"],
    # Co-occurrence of capitalized Entrata JSON keys is more discriminating
    # than ``"MinRent"`` alone, which RentCafe pages sometimes also emit.
    # ``application-type=prospect_portal`` and ``entrata.com/css/?template=``
    # mark vanity-domain Prospect Portal sites whose homepage embeds the
    # Entrata leasing widget without exposing the JSON keys until the
    # floorplans tab renders.
    html_fingerprints=[
        '"MinRent":',
        '"FloorplanId":',
        '"AvailableCount":',
        "application-type=prospect_portal",
        "entrata.com/css/?template=",
        "entrata.com/MLv3/",
    ],
    parser=parse_entrata_floorplans,
    pages=[],  # discover_pages above returns the homepage as the entry point
)
