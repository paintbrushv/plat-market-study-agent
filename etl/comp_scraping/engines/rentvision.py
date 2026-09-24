"""RentVision leasing engine.

RentVision sites (e.g. sedonaparkapts.com, willowparkirving.com,
liveatsummergate.com) ship server-rendered ``<li class="floorplanItem">``
cards. Detection: ``<meta name="author" content="RentVision" />`` plus the
``floorplanItem`` class. No Playwright required.
"""

from __future__ import annotations

import re
from typing import Any

from etl.comp_scraping.engines._base import SimpleEngine
from etl.comp_scraping.types import PageSpec


def _to_float(text: str | None) -> float:
    if not text:
        return 0.0
    cleaned = re.sub(r"[^\d.]", "", str(text))
    return float(cleaned) if cleaned else 0.0


def _first_dollar(text: str | None) -> float:
    """Extract the first ``$X,XXX(.YY)?`` amount in ``text`` as a float.

    RentVision's ``.priceFullValue`` block sometimes renders ranges as
    ``$1,223to-$1,249`` (no separator between the two dollar amounts).
    A naive ``_to_float`` strip-and-parse concatenates the digits.
    """

    if not text:
        return 0.0
    m = re.search(r"\$\s*([\d,]+(?:\.\d+)?)", str(text))
    if not m:
        return 0.0
    cleaned = m.group(1).replace(",", "")
    try:
        return float(cleaned)
    except ValueError:
        return 0.0


def _last_dollar(text: str | None) -> float:
    """Extract the last ``$X,XXX(.YY)?`` amount in ``text``."""

    if not text:
        return 0.0
    matches = re.findall(r"\$\s*([\d,]+(?:\.\d+)?)", str(text))
    if not matches:
        return 0.0
    try:
        return float(matches[-1].replace(",", ""))
    except ValueError:
        return 0.0


def parse_rentvision_floorplans(html_text: str) -> dict[str, Any]:
    """Parse RentVision ``li.floorplanItem`` cards from rendered HTML.

    Returns Shape A (``available_units`` keyed). One ``available_unit`` row
    per floorplan; ``available_count`` is derived from the availability text
    (``Available [Date]`` → ``1``; ``Sign Waitlist`` → ``0``).
    """

    try:
        from bs4 import BeautifulSoup
    except ImportError as exc:  # pragma: no cover
        raise ValueError(f"BeautifulSoup4 required for RentVision parser: {exc}") from exc

    if 'content="RentVision"' not in html_text and "floorplanItem" not in html_text:
        raise ValueError("No RentVision markers found")

    soup = BeautifulSoup(html_text, "html.parser")
    items = soup.select("li.floorplanItem")
    if not items:
        raise ValueError("No floorplanItem cards rendered")

    unit_rows: list[dict[str, Any]] = []
    for li in items:
        fid_raw = li.get("id") or ""
        fid = fid_raw.replace("floorplan_", "") if fid_raw else None

        beds_attr = li.get("data-bedrooms")
        beds = int(beds_attr) if beds_attr and str(beds_attr).isdigit() else 0
        if beds == 0:
            bed_el = li.select_one(".floorplanBeds .statValue")
            if bed_el:
                beds = int(_to_float(bed_el.get_text()))

        bath_el = li.select_one(".floorplanBaths .statValue")
        baths = _to_float(bath_el.get_text()) if bath_el else 1.0

        sqft_el = li.select_one(".floorplanSquareFootage .statValue")
        sqft = _to_float(sqft_el.get_text()) if sqft_el else 0.0

        name_el = li.select_one(".floorplanName a.floorplanNameAnchor") or li.select_one(
            ".floorplanName h3"
        )
        name = name_el.get_text(strip=True) if name_el else f"{beds}BR/{baths:.0f}BA"

        # Rent: prefer explicit min/max range when present, else parse the
        # combined ``.priceFullValue`` text. Some sites render ranges as
        # ``$1,223to-$1,249`` (no separator), so extract dollar amounts
        # individually rather than strip-and-parse the whole string.
        rent_min = 0.0
        rent_max = 0.0
        min_el = li.select_one(".priceFullValue .priceMinValue")
        max_el = li.select_one(".priceFullValue .priceMaxValue")
        override_el = li.select_one(".priceFullValue .priceValueOverride")
        full_el = li.select_one(".priceFullValue")
        if min_el:
            rent_min = _first_dollar(min_el.get_text())
            rent_max = _first_dollar(max_el.get_text()) if max_el else rent_min
        elif override_el:
            rent_min = _first_dollar(override_el.get_text())
            rent_max = rent_min
        elif full_el:
            text = full_el.get_text()
            rent_min = _first_dollar(text)
            rent_max = _last_dollar(text) or rent_min

        avail_el = li.select_one(".floorplanAvailability")
        avail_text = avail_el.get_text(strip=True) if avail_el else ""
        is_waitlist = avail_el is not None and "waitlist" in (avail_el.get("class") or [])
        avail_count = 0 if is_waitlist or "Waitlist" in avail_text else 1

        avail_date = None
        m = re.search(r"Available\s+on\s+([A-Za-z]+\s+\d+,?\s+\d{4})", avail_text)
        if m:
            avail_date = m.group(1)

        apply_el = li.select_one("a.floorplanDetailsBtn")
        apply_url = apply_el.get("href") if apply_el else None

        if rent_min > 0 or sqft > 0:
            unit_rows.append(
                {
                    "unit_number": None,
                    "beds": beds,
                    "baths": baths,
                    "sqft": sqft,
                    "rent": rent_min,
                    "rent_max": rent_max,
                    "available_date": avail_date,
                    "available_count": avail_count,
                    "apply_url": apply_url,
                    "floorplan_id": fid,
                    "floorplan_name": name,
                }
            )

    if not unit_rows:
        raise ValueError("RentVision cards present but no rent data parsed")

    # Specials banner — RentVision occasionally surfaces site-wide specials
    specials: list[str] = []
    for pat in (r"(\d+\s*weeks?\s*free[^<.]{0,120})", r"(\d+\s*months?\s*free[^<.]{0,120})"):
        for sm in re.finditer(pat, html_text, re.IGNORECASE):
            snippet = re.sub(r"\s+", " ", sm.group(1)).strip()
            if snippet not in specials:
                specials.append(snippet)

    return {
        "available_units": unit_rows,
        "floorplan_summary": [],
        "specials": specials[:5],
        "parser": "rentvision",
    }


RENTVISION_ENGINE = SimpleEngine(
    name="rentvision",
    url_substrings=[],
    html_fingerprints=[
        'content="RentVision"',
        'li class="floorplanItem"',
        "floorplanNameAnchor",
    ],
    parser=parse_rentvision_floorplans,
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
