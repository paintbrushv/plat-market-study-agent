"""Parser for simple static floorplan pages.

Some small property sites render floorplan cards directly in HTML without a
PMS API. This engine captures bed/bath/sf and any visible rent, while leaving
``Please Call`` rents null so downstream evidence quality stays conservative.
"""

from __future__ import annotations

import re
from typing import Any

from bs4 import BeautifulSoup

from etl.comp_scraping.engines._base import SimpleEngine
from etl.comp_scraping.types import PageSpec


def _money(text: str) -> float | None:
    match = re.search(r"\$\s*([0-9][0-9,]*)", text)
    if not match:
        return None
    return float(match.group(1).replace(",", ""))


def _floorplan_name(heading: str) -> str:
    cleaned = re.sub(r"\s+", " ", heading).strip()
    match = re.match(r"(.+?)\s+(?:One|Two|Three|Studio)\s+Bedroom\b", cleaned, re.I)
    if match:
        return match.group(1).strip()
    return cleaned.split(" - ")[0].strip()


def parse_static_floorplans(html: str) -> dict[str, Any]:
    soup = BeautifulSoup(html or "", "html.parser")
    text = soup.get_text("\n")
    if "Bedroom(s)" not in text and "Sq. Ft" not in text:
        raise ValueError("static floorplan fingerprint missing")

    lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines()]
    lines = [line for line in lines if line]

    floorplans: list[dict[str, Any]] = []
    current_name: str | None = None
    beds: int | None = None
    baths: float | None = None
    sqft: float | None = None
    rent: float | None = None

    def flush() -> None:
        nonlocal current_name, beds, baths, sqft, rent
        if current_name and beds is not None and baths is not None and sqft is not None:
            floorplans.append(
                {
                    "floorplan_name": _floorplan_name(current_name),
                    "beds": beds,
                    "baths": baths,
                    "sqft": sqft,
                    "rent_min": rent,
                    "rent_max": rent,
                }
            )
        current_name = None
        beds = None
        baths = None
        sqft = None
        rent = None

    for line in lines:
        if re.search(r"\b(?:One|Two|Three|Studio)\s+Bedroom\b", line, re.I) and "Sq. Ft" in line:
            flush()
            current_name = line
            inline_beds = re.search(r"\b(One|Two|Three)\s+Bedroom\b", line, re.I)
            if inline_beds:
                beds = {"one": 1, "two": 2, "three": 3}[inline_beds.group(1).lower()]
            elif re.search(r"\bStudio\b", line, re.I):
                beds = 0
            inline_baths = re.search(r"\b(One|Two|Three)\s+Bath\b", line, re.I)
            if inline_baths:
                baths = float({"one": 1, "two": 2, "three": 3}[inline_baths.group(1).lower()])
            inline_sqft = re.search(r"([0-9][0-9,]*)\s*Sq\.?\s*Ft", line, re.I)
            if inline_sqft:
                sqft = float(inline_sqft.group(1).replace(",", ""))
            continue
        if line.startswith("Bedroom(s)"):
            match = re.search(r"([0-9]+)", line)
            if match:
                beds = int(match.group(1))
        elif line.startswith("Bathroom(s)"):
            match = re.search(r"([0-9]+(?:\.[0-9]+)?)", line)
            if match:
                baths = float(match.group(1))
        elif line.startswith("Sq. Ft"):
            match = re.search(r"([0-9][0-9,]*)", line)
            if match:
                sqft = float(match.group(1).replace(",", ""))
        elif line.startswith("Rent"):
            rent = _money(line)
    flush()

    return {"platform": "static_floorplans", "floorplans": floorplans, "units": [], "specials": []}


STATIC_FLOORPLANS_ENGINE = SimpleEngine(
    name="static_floorplans",
    url_substrings=["cambridgecourttx.com"],
    html_fingerprints=["Bedroom(s)", "Sq. Ft"],
    parser=parse_static_floorplans,
    pages=[
        PageSpec(
            name="floorplans",
            url_template="{base}",
            fetch_method="auto",
            parser="floorplans",
            required=True,
        )
    ],
    confidence="medium",
)
