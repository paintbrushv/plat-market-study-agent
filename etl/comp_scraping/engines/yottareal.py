"""Yottareal / Adara Portal engine.

The user-facing SPA at ``adaraportal.yottareal.com/dba/floorplans?dbaid=<id>``
calls a public REST API at
``https://residentapis.yottareal.com/api/DBA/GetFloorPlans/<dbaid>`` that
returns clean JSON (``unitTypeModel`` + ``hotSheetUnitsModel``). We bypass
the SPA entirely by hitting the API directly.

Engine subclasses :class:`SimpleEngine` to override :meth:`discover_pages`,
extracting ``dbaid`` from the comp's base URL and constructing the absolute
API URL — :func:`etl.comp_scraping.dispatch._interpolate` accepts absolute
URLs verbatim.
"""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from typing import Any
from urllib.parse import parse_qs, urlparse

from etl.comp_scraping.engines._base import SimpleEngine
from etl.comp_scraping.types import PageSpec

_API_TEMPLATE = "https://residentapis.yottareal.com/api/DBA/GetFloorPlans/{dbaid}"


def _extract_dbaid(url: str) -> str | None:
    """Return the ``dbaid`` query param from a Yottareal portal URL."""

    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    if "dbaid" in qs and qs["dbaid"]:
        return str(qs["dbaid"][0])
    m = re.search(r"GetFloorPlans/(\d+)", url)
    return m.group(1) if m else None


def _xml_to_payload(text: str) -> dict[str, Any]:
    """Convert the WCF XML form (default when Accept header is text/html) to
    the same shape the JSON endpoint returns. Each ``<UnitTypeModel>`` /
    ``<HotSheetUnitsModel>`` child becomes a dict whose keys are the inner
    XML element local names (camel-cased to match the JSON contract)."""

    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise ValueError(f"Yottareal XML parse failed: {exc}") from exc

    def _strip(tag: str) -> str:
        return tag.rsplit("}", 1)[-1]

    def _tval(el: ET.Element) -> Any:  # noqa: ANN401
        t = (el.text or "").strip()
        if not t:
            return None
        # numeric coercion to mirror JSON typing
        if re.match(r"^-?\d+$", t):
            try:
                return int(t)
            except ValueError:
                return t
        if re.match(r"^-?\d+\.\d+$", t):
            try:
                return float(t)
            except ValueError:
                return t
        if t.lower() == "true":
            return True
        if t.lower() == "false":
            return False
        return t

    def _node_to_dict(node: ET.Element) -> dict[str, Any]:
        name = _strip(node.tag)
        # Lower-case first letter to match JSON camelCase keys (e.g. NumBedRooms → numBedRooms)
        return {name[:1].lower() + name[1:]: _tval(node)}

    out: dict[str, list[dict[str, Any]]] = {
        "bedroomTypeModel": [],
        "unitTypeModel": [],
        "hotSheetUnitsModel": [],
    }
    for collection in root:
        col_name = _strip(collection.tag)
        # Map XML collection tags to the JSON-contract keys
        target = (
            "bedroomTypeModel"
            if col_name == "bedroomTypeModel"
            else "unitTypeModel"
            if col_name == "unitTypeModel"
            else "hotSheetUnitsModel"
            if col_name == "hotSheetUnitsModel"
            else None
        )
        if target is None:
            continue
        for item in collection:
            row: dict[str, Any] = {}
            for child in item:
                key = _strip(child.tag)
                row[key[:1].lower() + key[1:]] = _tval(child)
            out[target].append(row)
    return out


def parse_yottareal_floorplans(text: str) -> dict[str, Any]:
    """Parse the GetFloorPlans response (JSON or WCF XML) into Shape A."""

    text_stripped = text.lstrip()
    if text_stripped.startswith("<"):
        payload = _xml_to_payload(text)
    else:
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Yottareal JSON parse failed: {exc}") from exc

    unit_types = payload.get("unitTypeModel") or []
    hot_sheet = payload.get("hotSheetUnitsModel") or []
    if not isinstance(unit_types, list) or not unit_types:
        raise ValueError("Yottareal payload missing unitTypeModel")

    # Group hot-sheet rows by dbaUnitTypeId for per-floorplan rent ranges + earliest avail
    by_fp: dict[Any, list[dict[str, Any]]] = {}
    for u in hot_sheet:
        if isinstance(u, dict):
            by_fp.setdefault(u.get("dbaUnitTypeId"), []).append(u)

    unit_rows: list[dict[str, Any]] = []
    specials: list[str] = []

    for ut in unit_types:
        if not isinstance(ut, dict):
            continue
        fid = ut.get("dbaUnitTypeId")
        beds = int(ut.get("numBedRooms") or ut.get("bedRooms") or 0)
        baths = float(ut.get("bathRooms") or 0)
        sqft = float(ut.get("area") or 0)
        avail_count = int(ut.get("availableUnitsCount") or 0)
        name = str(
            ut.get("dbaUnitType") or ut.get("unitTypeCode") or f"{beds}BR/{baths:.0f}BA"
        ).strip()

        special = str(ut.get("moveInSpecial") or "").strip()
        if special and special not in specials:
            specials.append(special)

        units_for_fp = by_fp.get(fid, [])
        rents = [float(u.get("rent") or 0) for u in units_for_fp if (u.get("rent") or 0) > 0]
        rent_min = min(rents) if rents else 0.0
        rent_max = max(rents) if rents else 0.0

        avail_date = None
        if units_for_fp:
            dates = [
                str(u.get("dateAvailable") or "") for u in units_for_fp if u.get("dateAvailable")
            ]
            if dates:
                avail_date = sorted(dates)[0]

        # Emit one row per available unit when we have hot-sheet detail; otherwise
        # one floorplan-level summary row.
        if units_for_fp:
            for u in units_for_fp:
                rent_u = float(u.get("rent") or 0)
                if rent_u <= 0:
                    continue
                unit_rows.append(
                    {
                        "unit_number": str(u.get("unitNumber") or "") or None,
                        "beds": beds,
                        "baths": baths,
                        "sqft": float(u.get("squareFeet") or sqft),
                        "rent": rent_u,
                        "rent_max": rent_u,
                        "available_date": str(u.get("dateAvailable") or "") or None,
                        "available_count": 1,
                        "apply_url": str(u.get("onlinePath") or "") or None,
                        "floorplan_id": str(fid) if fid is not None else None,
                        "floorplan_name": name,
                    }
                )
        elif sqft > 0:
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
                    "apply_url": None,
                    "floorplan_id": str(fid) if fid is not None else None,
                    "floorplan_name": name,
                }
            )

    if not unit_rows:
        raise ValueError("Yottareal payload parsed but no rent data")

    return {
        "available_units": unit_rows,
        "floorplan_summary": [],
        "specials": specials[:5],
        "parser": "yottareal",
    }


class _YottarealEngine(SimpleEngine):
    """Override ``discover_pages`` to point at the absolute API URL keyed by dbaid."""

    def discover_pages(self, base_url: str) -> list[PageSpec]:
        dbaid = _extract_dbaid(base_url)
        if not dbaid:
            # Fall back to the default — likely 404; surfaced as scrape failure.
            return list(self.pages) if self.pages else []
        return [
            PageSpec(
                name="floorplans",
                url_template=_API_TEMPLATE.format(dbaid=dbaid),
                fetch_method="auto",
                parser="floorplans",
                required=True,
            )
        ]


YOTTAREAL_ENGINE = _YottarealEngine(
    name="yottareal",
    url_substrings=[
        "adaraportal.yottareal.com",
        "residentapis.yottareal.com",
        "yottareal.com/dba",
    ],
    html_fingerprints=["adaraportal.yottareal.com", '"unitTypeModel":', '"hotSheetUnitsModel":'],
    parser=parse_yottareal_floorplans,
    pages=[],  # discover_pages constructs the API URL from dbaid
)
