"""RentCafe / Yardi leasing engine.

RentCafe ships floorplan data in three distinct shapes:

* Legacy JSON blob — ``"floorplans": [{...}]`` inline in the page (handled by
  the legacy ``parse_rentcafe_floorplans`` from the monolith).
* Modern card layout — DOM elements carrying ``data-selenium-id="Floorplan{N}*"``
  (handled by :func:`parse_rentcafe_card_floorplans`).
* **Per-floorplan detail layout** — the floorplans index page lists plans
  with rent ranges only; each plan links to a ``/floorplans/<slug>``
  detail page where the actual unit-level inventory lives. Detail pages
  emit one ``<tr class="unit-container">`` per available unit, plus an
  ``applyGAClick(<plan>, <beds>, <sqft>, <rent_min>, <rent_max>, <apt#>)``
  onclick that carries the full payload. We crawl those detail pages
  whenever the index parse yields zero units — this lifts unit counts
  on properties like The Ashton / Woodland Hills / Brookstone & Terrace
  that hide availability behind the per-floorplan tab.

The engine tries the JSON parser first and falls back to the card parser when
no JSON blob is present. This keeps all three layouts covered with the same
engine module.
"""

from __future__ import annotations

import re
from typing import Any

from etl.collect_comps_snapshot import parse_rentcafe_floorplans
from etl.comp_scraping.engines._base import SimpleEngine, _parse_specials_banner
from etl.comp_scraping.engines._rentcafe_card import (
    parse_rentcafe_action_button_floorplans,
    parse_rentcafe_card_floorplans,
    parse_yardi_fplist_floorplans,
)
from etl.comp_scraping.normalize import normalize_shape
from etl.comp_scraping.types import PageSpec, PartialResult

_DETAIL_URL_RE = re.compile(
    r'href="(https?://[^"]+/floorplans/[A-Za-z0-9._-]+)"'
)
# applyGAClick('1.1A', '1 Bed(s)', '582', '1051.00', '2149.00', '1101')
_GA_CLICK_RE = re.compile(
    r"applyGAClick\(\s*'([^']+)'\s*,\s*'(\d+)\s*Bed\(s\)'\s*,\s*'(\d+)'\s*,"
    r"\s*'([\d.]+)'\s*,\s*'([\d.]+)'\s*,\s*'([^']+)'\s*\)",
    re.IGNORECASE,
)


def _parse_rentcafe_either(html_text: str) -> dict[str, Any]:
    # Try parsers in order of specificity / data richness.
    last_error: Exception | None = None
    for parser in (
        parse_yardi_fplist_floorplans,
        parse_rentcafe_floorplans,
        parse_rentcafe_card_floorplans,
        parse_rentcafe_action_button_floorplans,
    ):
        try:
            return parser(html_text)
        except ValueError as exc:
            last_error = exc
            continue
    raise last_error or ValueError("No RentCafe variant matched")


def _parse_per_floorplan_units(html_text: str) -> list[dict[str, Any]]:
    """Extract unit rows from a RentCafe per-floorplan detail page.

    Reads the ``applyGAClick(...)`` onclick handlers, which carry the full
    payload for every unit listed on the page. One row per available unit.
    """

    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for m in _GA_CLICK_RE.finditer(html_text):
        plan_name, beds_str, sqft_str, rent_min_str, rent_max_str, apt = m.groups()
        if apt in seen:
            continue
        seen.add(apt)
        try:
            beds = int(beds_str)
            sqft = float(sqft_str)
            rent = float(rent_min_str)
            rent_max = float(rent_max_str)
        except ValueError:
            continue
        if rent <= 0:
            continue
        rows.append(
            {
                "unit_number": apt,
                "beds": beds,
                "baths": 1.0,  # RentCafe GA payload doesn't carry baths; default
                "sqft": sqft,
                "rent": rent,
                "rent_max": rent_max,
                "available_date": None,
                "available_count": 1,
                "apply_url": None,
                "floorplan_id": None,
                "floorplan_name": plan_name,
            }
        )
    return rows


class _RentCafeEngine(SimpleEngine):
    def parse_page(self, spec: PageSpec, content: Any) -> PartialResult:  # noqa: ANN401
        text = content if isinstance(content, str) else ""
        if spec.parser == "specials_banner":
            return _parse_specials_banner(spec.name, text)
        try:
            payload = _parse_rentcafe_either(text)
        except ValueError:
            return PartialResult(page_name=spec.name)
        normalised = normalize_shape(payload)
        return PartialResult(
            page_name=spec.name,
            floorplans=list(normalised.get("floorplans") or []),
            units=list(normalised.get("units") or []),
            specials=list(normalised.get("specials") or []),
            # Stash the raw HTML so ``merge`` can re-mine it for per-floorplan
            # detail-page URLs when the index returned zero units.
            raw_meta={
                "parser": payload.get("parser") or payload.get("platform"),
                "raw_html": text if spec.name == "floorplans" else "",
            },
        )

    def merge(self, partials: list[PartialResult]) -> Any:  # noqa: ANN401
        """Crawl per-floorplan detail pages when the index returned zero units.

        The base merge produces floorplan-level rollups; on RentCafe variants
        whose index page lists plans without unit detail (e.g. The Ashton),
        ``available_count`` is zero and ``units`` is empty. We detect that
        condition, walk the detail-page links extracted from the index HTML,
        and replace the empty unit list with the aggregated per-unit rows.
        """

        from etl.http_client import fetch_html  # local import to avoid cycles

        merged = super().merge(partials)
        if merged.units:
            return merged

        # Pull the index HTML out of the partials' raw_meta (stashed by parse_page).
        index_html = ""
        for p in partials:
            if p.page_name == "floorplans":
                index_html = (p.raw_meta or {}).get("raw_html", "") or ""
                if index_html:
                    break
        if not index_html:
            return merged

        urls = sorted(set(_DETAIL_URL_RE.findall(index_html)))
        # ``/floorplans`` itself is in the link set; drop the index URL
        urls = [u for u in urls if not u.rstrip("/").endswith("/floorplans")]
        if not urls:
            return merged

        expected_units = 0
        for fp in merged.floorplans:
            try:
                expected_units += int(fp.get("available_units") or 0)
            except (TypeError, ValueError):
                continue

        unit_rows: list[dict[str, Any]] = []
        seen_units: set[str] = set()
        # Detail pages are useful but can be slow or bot-sensitive. Once the
        # rendered index reveals detail URLs, those pages usually return their
        # applyGAClick unit payloads with ordinary browser headers, so avoid
        # launching a fresh browser per floorplan.
        for u in urls[:16]:
            status, page_html = fetch_html(u, method="auto", timeout_s=12)
            if status != 200 or not page_html:
                continue
            for row in _parse_per_floorplan_units(page_html):
                unit_number = str(row.get("unit_number") or "")
                if unit_number and unit_number in seen_units:
                    continue
                if unit_number:
                    seen_units.add(unit_number)
                unit_rows.append(row)
            if expected_units and len(unit_rows) >= expected_units:
                break

        if not unit_rows:
            return merged

        # Re-aggregate via normalize_shape so floorplans rollup picks up the
        # newly-discovered unit-level inventory.
        re_payload = normalize_shape(
            {
                "available_units": unit_rows,
                "specials": merged.specials,
                "parser": "rentcafe_perfp",
            }
        )
        merged.units = list(re_payload.get("units") or [])
        merged.floorplans = list(re_payload.get("floorplans") or merged.floorplans)
        return merged


RENTCAFE_ENGINE = _RentCafeEngine(
    name="rentcafe",
    url_substrings=["rentcafe.com", "rent-cafe", "yardi.com", "logansmillliving.com"],
    html_fingerprints=[
        '"floorplans":[{',
        '"floorplans":',
        "rentcafe",
        'data-selenium-id="Floorplan',
        "data-floorplan-name=",
        "ysi.floorplansList",
    ],
    parser=_parse_rentcafe_either,
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
        PageSpec(
            name="specials_page",
            url_template="{base}/specials/",
            fetch_method="auto",
            parser="specials_banner",
        ),
    ],
)
