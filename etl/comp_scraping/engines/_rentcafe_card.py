"""Parsers for non-XHR RentCafe / Yardi page shapes.

RentCafe sites render floorplan data in three observed shapes:

1. Modern DOM cards keyed by ``data-selenium-id="Floorplan{N}{Field}"``
   (handled by :func:`parse_rentcafe_card_floorplans`).
2. Inline ``ysi.fpList = [{...}]`` JavaScript array with capitalized-key
   floorplan dicts (handled by :func:`parse_yardi_fplist_floorplans`).
3. Legacy ``"floorplans": [{...}]`` JSON blob (handled by the existing
   ``parse_rentcafe_floorplans`` in the monolith).

The engine tries them in order; the first to extract priced rows wins.
"""

from __future__ import annotations

import json
import re
from typing import Any

from etl.comp_scraping._coerce import coerce_float, coerce_int

_BEDS_LITERAL_TO_INT = {
    "studio": 0,
    "efficiency": 0,
    "0": 0,
}


def _inner_text(html_text: str, selenium_id: str) -> str | None:
    """Return the text inside the first element carrying the given
    ``data-selenium-id`` attribute, or ``None`` if absent. Tolerates either
    quoting style and any element type."""

    pattern = re.compile(
        r'data-selenium-id=["\']' + re.escape(selenium_id) + r'["\'][^>]*>([\s\S]*?)</',
        re.IGNORECASE,
    )
    m = pattern.search(html_text)
    if not m:
        return None
    text = re.sub(r"<[^>]+>", " ", m.group(1))
    return re.sub(r"\s+", " ", text).strip() or None


def _coerce_beds(text: str | None) -> int:
    if not text:
        return 0
    cleaned = text.strip().lower()
    if cleaned in _BEDS_LITERAL_TO_INT:
        return _BEDS_LITERAL_TO_INT[cleaned]
    m = re.search(r"\d+", cleaned)
    return int(m.group(0)) if m else 0


def _parse_rent(text: str | None) -> tuple[float, float]:
    if not text:
        return 0.0, 0.0
    nums = [coerce_float(s) for s in re.findall(r"\$?[\d,]+(?:\.\d+)?", text)]
    nums = [n for n in nums if n > 0]
    if not nums:
        return 0.0, 0.0
    return min(nums), max(nums)


def _parse_availability(text: str | None) -> int:
    if not text:
        return 0
    if "contact for availability" in text.lower() or "waitlist" in text.lower():
        return 0
    m = re.search(r"\b(\d+)\b", text)
    return int(m.group(1)) if m else 0


def parse_rentcafe_card_floorplans(html_text: str) -> dict[str, Any]:
    """Parse RentCafe ``data-selenium-id="Floorplan*"`` cards.

    Raises :class:`ValueError` when no Floorplan-prefixed selenium IDs are
    found, so the dispatcher's quality gate / fallback can engage.
    """

    indices = sorted(
        {int(m.group(1)) for m in re.finditer(r'Floorplan(\d+)(?:Name|Beds|Rent|SqFt)', html_text)}
    )
    if not indices:
        raise ValueError("No RentCafe Floorplan cards found")

    def _field(idx: int, attr: str) -> str | None:
        return _inner_text(html_text, f"Floorplan{idx}{attr}") or _inner_text(
            html_text, f"Floorplan{idx}{attr}Mobile"
        )

    floorplans: list[dict[str, Any]] = []
    for idx in indices:
        name = _field(idx, "Name") or f"Plan {idx}"
        beds = _coerce_beds(_field(idx, "Beds"))
        baths = coerce_float(_field(idx, "Baths"), default=1.0)
        sqft_text = _field(idx, "SqFt") or ""
        sqft_nums = [coerce_float(s) for s in re.findall(r"\d[\d,]*", sqft_text)]
        sqft_nums = [n for n in sqft_nums if n > 0]
        sqft = max(sqft_nums) if sqft_nums else 0.0  # top of range

        rent_text = _field(idx, "Rent") or _field(idx, "Price") or ""
        rent_min, rent_max = _parse_rent(rent_text)
        if rent_min == 0:
            continue

        avail_text = _field(idx, "Availability")
        available_units = _parse_availability(avail_text)

        floorplans.append(
            {
                "floorplan_name": name,
                "beds": beds,
                "baths": baths,
                "sqft": sqft,
                "rent_min": rent_min,
                "rent_max": rent_max,
                "available_units": available_units,
            }
        )

    if not floorplans:
        raise ValueError("RentCafe Floorplan cards found but no priced rows extracted")

    return {
        "platform": "rentcafe_card",
        "floorplans": floorplans,
        "units": [],
        "specials": [],
    }


# Yardi SiteInsight inline arrays come under several variable names,
# all camel-cased and rooted at ``ysi`` or a bare local. Cover the
# observed variants: ``fpList``, ``floorplansList``, ``floorplanList``.
_FPLIST_OPEN = re.compile(
    r"(?:ysi\.)?(?:fpList|floorplansList|floorplanList)\s*=\s*(\[)"
)
_DECODER = json.JSONDecoder()


def parse_yardi_fplist_floorplans(html_text: str) -> dict[str, Any]:
    """Parse the ``ysi.fpList = [{...}]`` Yardi SiteInsight inline array.

    Each element has fields like ``Id``, ``Beds``, ``Baths``, ``MinSqFt``,
    ``MaxSqFt``, ``Rentmin``, ``Rentmax``, ``Rentcol`` (a "$X-$Y" range
    string), ``Name``, ``UnitsAvailable``. Raises :class:`ValueError` when
    the variable is absent so the engine can fall through.
    """

    m = _FPLIST_OPEN.search(html_text)
    if not m:
        raise ValueError("No ysi.fpList array found")
    arr_start = m.start(1)
    try:
        arr, _end = _DECODER.raw_decode(html_text, arr_start)
    except (json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"ysi.fpList decode failed: {exc}") from exc
    if not isinstance(arr, list):
        raise ValueError("ysi.fpList parsed but not a list")

    floorplans: list[dict[str, Any]] = []
    for fp in arr:
        if not isinstance(fp, dict):
            continue
        rent_min = _yardi_rent(fp.get("MinRent") or fp.get("Rentmin"))
        rent_max = _yardi_rent(fp.get("MaxRent") or fp.get("Rentmax")) or rent_min
        rentcol = _yardi_rent_range(fp.get("Rentcol"))
        if rentcol:
            rent_min = rent_min or rentcol[0]
            rent_max = max(rent_max, rentcol[1])
        if rent_min == 0:
            continue
        floorplans.append(
            {
                "floorplan_name": str(
                    fp.get("Name") or fp.get("FloorplanName") or fp.get("FloorplanCode") or ""
                ),
                "beds": coerce_int(fp.get("Beds")),
                "baths": coerce_float(fp.get("Baths"), default=1.0),
                "sqft": coerce_float(fp.get("MaxSqFt") or fp.get("MinSqFt")),
                "rent_min": rent_min,
                "rent_max": rent_max,
                "available_units": coerce_int(
                    fp.get("AvailableCount") or fp.get("UnitsAvailable")
                ),
            }
        )

    if not floorplans:
        raise ValueError("ysi.fpList parsed but no priced rows extracted")

    return {
        "platform": "yardi_fplist",
        "floorplans": floorplans,
        "units": [],
        "specials": [],
    }


def _yardi_rent(value: Any) -> float:  # noqa: ANN401
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    nums = re.findall(r"[\d,]+(?:\.\d+)?", str(value))
    return coerce_float(nums[0]) if nums else 0.0


def _yardi_rent_range(value: Any) -> tuple[float, float] | None:  # noqa: ANN401
    if not isinstance(value, str):
        return None
    nums = [coerce_float(n) for n in re.findall(r"[\d,]+(?:\.\d+)?", value)]
    nums = [n for n in nums if n > 0]
    if not nums:
        return None
    return min(nums), max(nums)


# Apply-button anchors on RentCafe-driven sites carry per-floorplan
# attributes that are richer than the surrounding card text. Scanning
# these tags works on sites where the JSON variable / selenium-id-card
# extractors miss (e.g. Parkside Apartments, Birmingham).
_APPLY_BTN_ATTRS = re.compile(
    r"<a\b[^>]*?data-floorplan-name=\"([^\"]+)\"[^>]*?>",
    re.IGNORECASE,
)


def parse_rentcafe_action_button_floorplans(html_text: str) -> dict[str, Any]:
    """Parse RentCafe apply-now ``<a>`` anchors that carry per-plan
    attributes ``data-floorplan-name``, ``data-floorplan-size``,
    ``data-floorplan-sqft``, ``data-floorplan-price``.
    """

    matches = list(_APPLY_BTN_ATTRS.finditer(html_text))
    if not matches:
        raise ValueError("No RentCafe apply-button anchors found")

    floorplans: list[dict[str, Any]] = []
    seen: set[str] = set()
    for match in matches:
        full_tag = _enclosing_anchor(html_text, match.start())
        attrs = dict(re.findall(r'data-([\w-]+)="([^"]*)"', full_tag))
        name = attrs.get("floorplan-name") or ""
        if not name or name in seen:
            continue

        sqft_raw = attrs.get("floorplan-sqft") or ""
        sqft = coerce_float(re.sub(r"[^\d.]", "", sqft_raw.split("-")[0]) or "0")

        price_raw = attrs.get("floorplan-price") or ""
        rent_lo, rent_hi = _yardi_rent_range(price_raw) or (0.0, 0.0)
        if rent_lo == 0:
            continue
        seen.add(name)

        floorplans.append(
            {
                "floorplan_name": name,
                "beds": coerce_int(attrs.get("floorplan-size")),
                "baths": coerce_float(attrs.get("floorplan-bath"), default=1.0),
                "sqft": sqft,
                "rent_min": rent_lo,
                "rent_max": rent_hi,
                "available_units": coerce_int(attrs.get("floorplan-availability")),
            }
        )

    if not floorplans:
        raise ValueError(
            "RentCafe apply-button anchors found but no priced rows extracted"
        )

    return {
        "platform": "rentcafe_action_btn",
        "floorplans": floorplans,
        "units": [],
        "specials": [],
    }


def _enclosing_anchor(html_text: str, idx: int) -> str:
    """Return the full ``<a ...>`` opening tag enclosing ``idx``."""

    start = html_text.rfind("<a", 0, idx + 1)
    if start < 0:
        return ""
    end = html_text.find(">", idx)
    if end < 0:
        return ""
    return html_text[start : end + 1]
