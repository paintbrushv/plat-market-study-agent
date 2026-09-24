"""Collect vacancy, rent, and concession snapshots for Austin TX new-build (2025+) properties.

Tracks lease-up activity in new Class A supply as a demand signal for Terrace Cove's
competitive positioning against the Austin oversupply wave.

Modes
-----
  (default)               Scrape all properties with a direct_site_url in the manifest.
  --init-from-excel PATH  One-time bootstrap: create manifest from CoStar Excel export.
  --geocode               Geocode manifest rows missing lat/lon via Nominatim (1 req/sec).

Usage
-----
  uv run python etl/collect_new_builds_snapshot.py
  uv run python etl/collect_new_builds_snapshot.py \\
      --init-from-excel reports/austin-tx/new-builds/austin-new-builds.xlsx
  uv run python etl/collect_new_builds_snapshot.py --geocode
  uv run python etl/collect_new_builds_snapshot.py --date 2026-03-19
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import math
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from pathlib import Path
from typing import Any

import pandas as pd

# ---------------------------------------------------------------------------
# Shared utilities from collect_comps_snapshot (same etl/ directory)
# ---------------------------------------------------------------------------
sys.path.insert(0, str(Path(__file__).parent))
from collect_comps_snapshot import (  # noqa: E402
    parse_appfolio_config,
    parse_appfolio_listings,
    parse_g5_floorplans_plus,
    parse_resi_floorplans_and_units,
    safe_fetch_text,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Terrace Cove — subject property (6201 Sneed Cove, Austin TX 78744)
SUBJECT_LAT: float = 30.1853
SUBJECT_LON: float = -97.7412

DEFAULT_MANIFEST: Path = Path("reports/austin-tx/new-builds/new_builds_manifest.csv")
DEFAULT_HISTORY: Path = Path("reports/austin-tx/new-builds/new_builds_history.parquet")
DEFAULT_EXCEL: Path = Path("reports/austin-tx/new-builds/austin-new-builds.xlsx")

MANIFEST_COLUMNS: list[str] = [
    "property_name",
    "address",
    "city",
    "state",
    "zip",
    "submarket",
    "building_class",
    "year_built",
    "units",
    "stories",
    "costar_vacancy_pct",
    "lat",
    "lon",
    "distance_mi",
    "direct_site_url",
    "apartments_com_url",
    "scrape_platform",
    "first_scraped_date",
    "notes",
]

HISTORY_COLUMNS: list[str] = [
    "property_name",
    "address",
    "submarket",
    "building_class",
    "year_built",
    "total_units",
    "distance_mi",
    "costar_vacancy_pct",
    "unit_type",
    "beds",
    "sqft_avg",
    "face_rent",
    "effective_rent",
    "rent_psf",
    "units_available",
    "vacancy_pct_proxy",
    "concessions",
    "free_months",
    "status",
    "data_source",
]

# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------


def haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Straight-line distance in miles between two lat/lon points."""
    r = 3958.8
    lat1r, lon1r, lat2r, lon2r = (math.radians(x) for x in (lat1, lon1, lat2, lon2))
    dlat = lat2r - lat1r
    dlon = lon2r - lon1r
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1r) * math.cos(lat2r) * math.sin(dlon / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def nominatim_geocode(
    address: str, city: str, state: str, zip_code: str
) -> tuple[float, float] | None:
    """Geocode via Nominatim (OpenStreetMap). Returns (lat, lon) or None.

    Respects Nominatim usage policy: max 1 req/sec, descriptive User-Agent.
    Caller is responsible for rate-limiting between calls.
    """
    query = f"{address}, {city}, {state} {zip_code}"
    url = "https://nominatim.openstreetmap.org/search?" + urllib.parse.urlencode(
        {"q": query, "format": "json", "limit": 1, "countrycodes": "us"}
    )
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "market-study-agent/new-builds-tracker/0.1 "
                "(internal owner monitoring, non-commercial)"
            )
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            results = json.loads(resp.read().decode("utf-8"))
        if results:
            return float(results[0]["lat"]), float(results[0]["lon"])
    except Exception as exc:
        print(f"  Geocoding failed for '{query}': {exc}")
    return None


# ---------------------------------------------------------------------------
# Manifest I/O
# ---------------------------------------------------------------------------


def _manifest_int(row: dict[str, str], key: str) -> int | None:
    v = row.get(key, "").strip()
    return int(v) if v.isdigit() else None


def _manifest_float(row: dict[str, str], key: str) -> float | None:
    v = row.get(key, "").strip()
    try:
        return float(v) if v else None
    except ValueError:
        return None


def read_manifest(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def write_manifest(rows: list[dict[str, str]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=MANIFEST_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


# ---------------------------------------------------------------------------
# Manifest init from CoStar Excel export
# ---------------------------------------------------------------------------


def init_manifest_from_excel(excel_path: Path, manifest_path: Path) -> None:
    """Bootstrap new_builds_manifest.csv from a CoStar Excel export."""
    print(f"Reading Excel: {excel_path}")
    df = pd.read_excel(excel_path)

    def _s(col: str, r: pd.Series) -> str:  # type: ignore[type-arg]
        v = r.get(col)
        return str(v).strip() if v is not None and str(v) != "nan" else ""

    def _int_str(col: str, r: pd.Series) -> str:  # type: ignore[type-arg]
        v = r.get(col)
        try:
            return str(int(float(v))) if v is not None and str(v) != "nan" else ""
        except (ValueError, TypeError):
            return ""

    def _float_str(col: str, r: pd.Series, decimals: int = 4) -> str:  # type: ignore[type-arg]
        v = r.get(col)
        try:
            return str(round(float(v), decimals)) if v is not None and str(v) != "nan" else ""
        except (ValueError, TypeError):
            return ""

    rows: list[dict[str, str]] = []
    for _, row in df.iterrows():
        rows.append(
            {
                "property_name": _s("Property Name", row),
                "address": _s("Property Address", row),
                "city": _s("City", row),
                "state": _s("State", row),
                "zip": _s("Zip", row),
                "submarket": _s("Submarket Name", row),
                "building_class": _s("Building Class", row),
                "year_built": _int_str("Year Built", row),
                "units": _int_str("Number Of Units", row),
                "stories": _int_str("Number Of Stories", row),
                "costar_vacancy_pct": _float_str("Vacancy %", row),
                "lat": "",
                "lon": "",
                "distance_mi": "",
                "direct_site_url": "",
                "apartments_com_url": "",
                "scrape_platform": "",
                "first_scraped_date": "",
                "notes": "",
            }
        )

    write_manifest(rows, manifest_path)
    print(f"Wrote {len(rows)} properties to {manifest_path}")
    print("Next step: uv run python etl/collect_new_builds_snapshot.py --geocode")


# ---------------------------------------------------------------------------
# Geocoding pass
# ---------------------------------------------------------------------------


def geocode_manifest(manifest_path: Path) -> None:
    """Geocode manifest rows missing lat/lon via Nominatim. Rate-limited to 1 req/sec."""
    rows = read_manifest(manifest_path)
    if not rows:
        print(f"Manifest is empty: {manifest_path}")
        return

    to_geocode = [r for r in rows if not r.get("lat") or not r.get("lon")]
    print(f"{len(to_geocode)} properties need geocoding (of {len(rows)} total)")

    updated = 0
    for _i, row in enumerate(rows):
        if row.get("lat") and row.get("lon"):
            continue
        address = row.get("address", "")
        city = row.get("city", "")
        state = row.get("state", "")
        zip_code = row.get("zip", "")
        print(f"  [{updated + 1}/{len(to_geocode)}] {address}, {city}, {state} {zip_code}")
        result = nominatim_geocode(address, city, state, zip_code)
        if result:
            lat, lon = result
            row["lat"] = str(round(lat, 6))
            row["lon"] = str(round(lon, 6))
            dist = haversine_miles(SUBJECT_LAT, SUBJECT_LON, lat, lon)
            row["distance_mi"] = str(round(dist, 2))
            print(f"    → {lat:.4f}, {lon:.4f} ({row['distance_mi']} mi from Terrace Cove)")
            updated += 1
        else:
            print("    → geocoding failed, skipping")
        time.sleep(1.1)  # Nominatim: max 1 req/sec

    write_manifest(rows, manifest_path)
    print(f"Geocoded {updated} properties. Manifest saved to {manifest_path}")


# ---------------------------------------------------------------------------
# Concession helpers
# ---------------------------------------------------------------------------


def _extract_specials_text(html: str) -> list[str]:
    """Extract concession/specials text snippets from raw property page HTML."""
    specials: list[str] = []
    text = re.sub(r"<[^>]+>", " ", html)  # strip HTML tags
    text = re.sub(r"\s+", " ", text)  # normalise whitespace
    # Look for known concession phrases
    patterns = [
        r"[\w\s\$,!*]+(?:\d+\s*weeks?\s+free[\w\s\$,+!*]*)",
        r"[\w\s\$,!*]+(?:\d+(?:\.\d+)?\s*months?\s+free[\w\s\$,+!*]*)",
        r"[\w\s\$,!*]+(?:look\s+and\s+lease[\w\s\$,+!*]*)",
    ]
    for pat in patterns:
        m = re.search(pat, text, re.I)
        if m:
            snippet = m.group(0).strip()[:200]
            snippet = re.sub(r"\s+", " ", snippet)
            specials.append(snippet)
    return list(dict.fromkeys(specials))


def extract_free_months(specials_text: str) -> float:
    """Parse free months from concession text like '6 weeks free', '1 month free'."""
    text = specials_text.lower()
    m = re.search(r"(\d+(?:\.\d+)?)\s*weeks?\s+free", text)
    if m:
        return round(float(m.group(1)) / 4.33, 2)
    m = re.search(r"(\d+(?:\.\d+)?)\s*months?\s+free", text)
    if m:
        return float(m.group(1))
    return 0.0


# ---------------------------------------------------------------------------
# Unit-type aggregation
# ---------------------------------------------------------------------------


def beds_to_unit_type(beds: int) -> str:
    if beds == 0:
        return "Studio"
    return f"{beds}BR"


def aggregate_to_unit_types(
    unit_rows: list[dict[str, Any]],
    specials: list[str],
    total_units: int | None,
) -> list[dict[str, Any]]:
    """Aggregate per-unit scrape data into per-unit-type rows for the parquet.

    unit_rows must have keys: beds (int), sqft (float), rent (float).
    Handles Resi, AppFolio, and normalized G5 units.
    """
    concessions_text: str = "; ".join(specials) if specials else ""
    free_months = extract_free_months(concessions_text)

    by_beds: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for u in unit_rows:
        beds = int(float(u.get("beds") or 0))
        by_beds[beds].append(u)

    total_available = sum(len(v) for v in by_beds.values())

    rows: list[dict[str, Any]] = []
    for beds, units in sorted(by_beds.items()):
        rents = [float(u.get("rent") or 0) for u in units if float(u.get("rent") or 0) > 0]
        sqfts = [float(u.get("sqft") or 0) for u in units if float(u.get("sqft") or 0) > 0]
        face_rent = round(sum(rents) / len(rents), 2) if rents else None
        sqft_avg = round(sum(sqfts) / len(sqfts), 0) if sqfts else None

        effective_rent: float | None = None
        if face_rent is not None and free_months > 0:
            effective_rent = round(face_rent * (12 - free_months) / 12, 2)
        elif face_rent is not None:
            effective_rent = face_rent

        rent_psf: float | None = None
        if effective_rent and sqft_avg and sqft_avg > 0:
            rent_psf = round(effective_rent / sqft_avg, 2)

        vacancy_proxy: float | None = None
        if total_units and total_units > 0:
            vacancy_proxy = round(total_available / total_units, 4)

        rows.append(
            {
                "unit_type": beds_to_unit_type(beds),
                "beds": beds,
                "sqft_avg": sqft_avg,
                "face_rent": face_rent,
                "effective_rent": effective_rent,
                "rent_psf": rent_psf,
                "units_available": len(units),
                "vacancy_pct_proxy": vacancy_proxy,
                "concessions": concessions_text or None,
                "free_months": free_months if free_months > 0 else None,
            }
        )

    return rows


def normalize_g5_result(result: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    """Normalize G5 parse result to flat unit_rows (with beds/sqft) and specials list.

    G5 units lack beds/sqft — join with floorplans by floorplan_id.
    Falls back to expanding floorplan.available_units if units list is empty.
    """
    floorplans = result.get("floorplans") or []
    units = result.get("units") or []

    fp_lookup: dict[Any, dict[str, Any]] = {fp["floorplan_id"]: fp for fp in floorplans}

    # Collect unit-level specials (G5 stores specials per unit as a list)
    specials: list[str] = []
    for u in units:
        for s in u.get("specials") or []:
            if isinstance(s, str) and s.strip():
                specials.append(s.strip())
    specials = list(dict.fromkeys(specials))  # deduplicate preserving order

    if units:
        normalized = []
        for u in units:
            fp = fp_lookup.get(u.get("floorplan_id")) or {}
            normalized.append(
                {
                    "beds": fp.get("beds", 0),
                    "sqft": fp.get("sqft", 0),
                    "rent": u.get("rent") or fp.get("rent_min") or 0,
                }
            )
        return normalized, specials

    # Fallback: expand floorplans with available_units > 0
    expanded = []
    for fp in floorplans:
        avail = int(fp.get("available_units") or 0)
        if avail <= 0:
            continue
        for _ in range(avail):
            expanded.append(
                {
                    "beds": fp.get("beds", 0),
                    "sqft": fp.get("sqft", 0),
                    "rent": fp.get("rent_min") or 0,
                }
            )
    return expanded, specials


# ---------------------------------------------------------------------------
# RentCafe / Yardi parser
# ---------------------------------------------------------------------------


def parse_rentcafe_floorplans(url: str, html: str) -> dict[str, Any]:
    """Parse RentCafe/Yardi property.

    The /floorplans page embeds ``ysi.floorplansList`` — a JSON array of per-plan
    aggregates with beds, sqft, rent, and available unit count.
    """
    fp_html = html
    if "ysi.floorplansList" not in html:
        parsed_url = urllib.parse.urlparse(url)
        base_url = f"{parsed_url.scheme}://{parsed_url.netloc}"
        fp_fetch = safe_fetch_text(base_url + "/floorplans", timeout_s=30)
        if not fp_fetch["ok"]:
            return {
                "unit_rows": [],
                "specials": [],
                "errors": [f"Floorplans page HTTP {fp_fetch['status']}"],
            }
        fp_html = fp_fetch["text"]

    if "ysi.floorplansList" not in fp_html:
        return {"unit_rows": [], "specials": [], "errors": ["ysi.floorplansList not found"]}

    idx = fp_html.find("ysi.floorplansList = [")
    arr_start = fp_html.find("[", idx)
    arr_end = fp_html.find("];", arr_start) + 1
    try:
        floorplans = json.loads(fp_html[arr_start:arr_end])
    except json.JSONDecodeError as exc:
        return {"unit_rows": [], "specials": [], "errors": [f"JSON parse error: {exc}"]}

    unit_rows: list[dict[str, Any]] = []
    for fp in floorplans:
        avail = int(fp.get("AvailableCount") or 0)
        if avail <= 0 or fp.get("isCommercial"):
            continue
        beds = int(fp.get("Beds") or 0)
        min_sqft = float(fp.get("MinSqFt") or 0)
        max_sqft = float(fp.get("MaxSqFt") or 0)
        sqft = (min_sqft + max_sqft) / 2 if max_sqft > 0 else min_sqft
        rent = float(fp.get("MinRent") or 0)
        for _ in range(avail):
            unit_rows.append({"beds": beds, "sqft": sqft, "rent": rent})

    specials = _extract_specials_text(fp_html)
    # If no specials found on floorplans page, try the /specials page
    if not specials and "/specials" in fp_html:
        parsed_url2 = urllib.parse.urlparse(url)
        spec_url = f"{parsed_url2.scheme}://{parsed_url2.netloc}/specials"
        spec_fetch = safe_fetch_text(spec_url, timeout_s=20)
        if spec_fetch["ok"]:
            specials = _extract_specials_text(spec_fetch["text"])
    return {"unit_rows": unit_rows, "specials": specials, "errors": []}


# ---------------------------------------------------------------------------
# Jonah Digital / Greystar parser
# ---------------------------------------------------------------------------


def parse_jonah_digital_floorplans(url: str, html: str) -> dict[str, Any]:
    """Parse Greystar/Jonah Digital property.

    The main /floorplans/ page lists plan slugs as href patterns.
    Each /floorplans/<slug>/ page embeds a JSON object in a <script> tag
    with availability_count, bedrooms, square_feet, rent_min, and unit-level specials.
    """
    parsed_url = urllib.parse.urlparse(url)
    base_url = f"{parsed_url.scheme}://{parsed_url.netloc}"

    # Fetch the main floorplans page if not already the given URL
    fp_html = html
    if "/floorplans" not in url:
        fp_fetch = safe_fetch_text(base_url + "/floorplans/", timeout_s=30)
        if fp_fetch["ok"]:
            fp_html = fp_fetch["text"]

    # Extract plan slugs from href="/floorplans/<slug>/" patterns
    slugs = list(
        dict.fromkeys(re.findall(r"/floorplans/([a-z0-9]+(?:-[a-z0-9]+)*)/", fp_html))
    )
    # Filter out affordable variants (e.g. "1a-aff")
    market_slugs = [s for s in slugs if not s.endswith("-aff")]

    if not market_slugs:
        return {"unit_rows": [], "specials": [], "errors": ["No market-rate plan slugs found"]}

    unit_rows: list[dict[str, Any]] = []
    specials: list[str] = []
    errors: list[str] = []

    for slug in market_slugs[:12]:  # cap to avoid excessive requests
        plan_url = f"{base_url}/floorplans/{slug}/"
        plan_fetch = safe_fetch_text(plan_url, timeout_s=20)
        if not plan_fetch["ok"]:
            errors.append(f"Plan {slug}: HTTP {plan_fetch['status']}")
            continue

        plan_html = plan_fetch["text"]
        plan_data: dict[str, Any] | None = None

        # The JSON is the full content of the first <script> tag containing "availability_count"
        for sm in re.finditer(r"<script[^>]*>", plan_html):
            script_start = sm.end()
            script_end = plan_html.find("</script>", script_start)
            script_content = plan_html[script_start:script_end]
            if '"availability_count"' in script_content:
                try:
                    plan_data = json.loads(script_content)
                    break
                except json.JSONDecodeError:
                    pass

        if plan_data is None:
            errors.append(f"Plan {slug}: JSON not found")
            continue

        avail = int(plan_data.get("availability_count") or 0)
        if avail <= 0:
            continue

        _beds_raw = plan_data.get("bedrooms") or 0
        try:
            beds = int(_beds_raw)
        except (ValueError, TypeError):
            # "Studio" → 0
            beds = 0 if str(_beds_raw).lower().startswith("studio") else 0
        sqft = float(plan_data.get("square_feet") or 0)
        rent = float(plan_data.get("rent_min") or 0)

        for _ in range(avail):
            unit_rows.append({"beds": beds, "sqft": sqft, "rent": rent})

        # Collect unit-level specials
        for unit in plan_data.get("units") or []:
            for s in unit.get("specials") or []:
                text = s.get("description") or s.get("title") or "" if isinstance(s, dict) else str(s)
                if text.strip():
                    specials.append(text.strip())

    specials = list(dict.fromkeys(specials))
    # Also scan the main floorplans page HTML for specials text
    if not specials:
        specials = _extract_specials_text(fp_html)

    return {"unit_rows": unit_rows, "specials": specials, "errors": errors}


# ---------------------------------------------------------------------------
# Platform detection + scraping
# ---------------------------------------------------------------------------


def detect_and_scrape(url: str, run_date: str) -> dict[str, Any]:
    """Attempt to scrape a direct property URL using known platform parsers.

    Returns a dict with:
      platform  — detected platform name or error status
      unit_rows — list of {beds, sqft, rent} normalized units (empty if failed)
      specials  — list of concession strings
      errors    — list of error messages
    """
    result: dict[str, Any] = {
        "platform": None,
        "unit_rows": [],
        "specials": [],
        "errors": [],
    }

    fetch = safe_fetch_text(url, timeout_s=30)
    if not fetch["ok"]:
        status_code = int(fetch.get("status") or 0)
        result["errors"].append(f"HTTP {status_code}: {fetch.get('error', '')}")
        result["platform"] = "blocked" if status_code in (403, 429) else "fetch_error"
        return result

    html: str = fetch["text"]

    # --- Resi platform ---
    if "<floor-plan-units-modalv2" in html:
        try:
            parsed = parse_resi_floorplans_and_units(html)
            result["platform"] = "resi"
            result["unit_rows"] = parsed.get("units") or []
            result["specials"] = parsed.get("specials") or []
            return result
        except Exception as exc:
            result["errors"].append(f"Resi parse failed: {exc}")

    # --- G5 Floor Plans Plus ---
    # parse_g5_floorplans_plus fetches the URL internally, so we call it directly.
    if 'id="floor-plans-plus-config"' in html:
        try:
            parsed = parse_g5_floorplans_plus(url, run_date)
            unit_rows, specials = normalize_g5_result(parsed)
            result["platform"] = "g5"
            result["unit_rows"] = unit_rows
            result["specials"] = specials
            return result
        except Exception as exc:
            result["errors"].append(f"G5 parse failed: {exc}")

    # --- AppFolio ---
    if "Appfolio.Listing(" in html:
        try:
            cfg = parse_appfolio_config(html)
            if cfg:
                # Strip any existing scheme from host_url before prepending https://
                host = re.sub(r"^https?://", "", cfg["host_url"].strip("/"))
                listing_url = "https://{host}/listings?{qs}".format(
                    host=host,
                    qs=urllib.parse.urlencode({"filters[property_list]": cfg["property_group"]}),
                )
                listing_fetch = safe_fetch_text(listing_url, timeout_s=30)
                if listing_fetch["ok"]:
                    units = parse_appfolio_listings(listing_fetch["text"])
                    result["platform"] = "appfolio"
                    result["unit_rows"] = units
                    return result
                result["errors"].append("AppFolio listings fetch failed")
            else:
                result["errors"].append("AppFolio config not found in page")
        except Exception as exc:
            result["errors"].append(f"AppFolio parse failed: {exc}")

    # --- RentCafe / Yardi ---
    if "resource.rentcafe.com" in html or "cdngeneralmvc.rentcafe.com" in html:
        try:
            parsed_rc = parse_rentcafe_floorplans(url, html)
            if parsed_rc["unit_rows"] or not parsed_rc["errors"]:
                result["platform"] = "rentcafe"
                result["unit_rows"] = parsed_rc["unit_rows"]
                result["specials"] = parsed_rc["specials"]
                return result
            result["errors"].extend(parsed_rc["errors"])
        except Exception as exc:
            result["errors"].append(f"RentCafe parse failed: {exc}")
        result["platform"] = "rentcafe"
        return result

    # --- Jonah Digital / Greystar ---
    if "JonahWidget" in html:
        try:
            parsed_jd = parse_jonah_digital_floorplans(url, html)
            if parsed_jd["unit_rows"] or not parsed_jd["errors"]:
                result["platform"] = "jonah_digital"
                result["unit_rows"] = parsed_jd["unit_rows"]
                result["specials"] = parsed_jd["specials"]
                return result
            result["errors"].extend(parsed_jd["errors"])
        except Exception as exc:
            result["errors"].append(f"Jonah Digital parse failed: {exc}")
        result["platform"] = "jonah_digital"
        return result

    # --- Agency Fifty3 (Entrata/Yardi backend — floor plans are JS-rendered) ---
    if "aF3Vars" in html:
        result["platform"] = "agency_fifty3"
        result["errors"].append("Agency Fifty3: floor plan data is JS-rendered — use WebSearch fallback")
        return result

    result["platform"] = "unknown_platform"
    result["errors"].append("No supported platform detected (Resi / G5 / AppFolio / RentCafe / Jonah / AF3)")
    return result


# ---------------------------------------------------------------------------
# Parquet upsert
# ---------------------------------------------------------------------------


def upsert_to_parquet(
    records: list[dict[str, Any]], scrape_date: str, path: Path
) -> None:
    """Upsert records into history parquet, keyed by (scrape_date, property_name, unit_type)."""
    records_with_date = [{"scrape_date": scrape_date, **r} for r in records]
    new_df = pd.DataFrame(records_with_date)

    all_cols = ["scrape_date"] + HISTORY_COLUMNS
    for col in all_cols:
        if col not in new_df.columns:
            new_df[col] = None
    new_df = new_df[all_cols].replace("", None)

    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        existing = pd.read_parquet(path)
        if "scrape_date" in existing.columns:
            existing = existing[existing["scrape_date"] != scrape_date]
        combined = pd.concat([existing, new_df], ignore_index=True)
    else:
        combined = new_df

    combined.to_parquet(path, index=False)
    print(f"Wrote {len(new_df)} rows → {path} ({len(combined)} total rows)")


# ---------------------------------------------------------------------------
# Main scrape loop
# ---------------------------------------------------------------------------


def _null_row(base: dict[str, Any], status: str, data_source: str) -> dict[str, Any]:
    """Return a placeholder parquet row with null rent/vacancy values."""
    return {
        **base,
        "unit_type": "property_avg",
        "beds": None,
        "sqft_avg": None,
        "face_rent": None,
        "effective_rent": None,
        "rent_psf": None,
        "units_available": None,
        "vacancy_pct_proxy": None,
        "concessions": None,
        "free_months": None,
        "status": status,
        "data_source": data_source,
    }


def scrape_all(manifest_path: Path, history_path: Path, run_date: str) -> None:
    rows = read_manifest(manifest_path)
    if not rows:
        print(f"Manifest is empty: {manifest_path}")
        return

    new_records: list[dict[str, Any]] = []
    stats: dict[str, int] = {"success": 0, "no_url": 0, "failed": 0}

    for i, prop in enumerate(rows):
        name = prop.get("property_name", "")
        direct_url = prop.get("direct_site_url", "").strip()

        base: dict[str, Any] = {
            "property_name": name,
            "address": prop.get("address", ""),
            "submarket": prop.get("submarket", ""),
            "building_class": prop.get("building_class", ""),
            "year_built": _manifest_int(prop, "year_built"),
            "total_units": _manifest_int(prop, "units"),
            "distance_mi": _manifest_float(prop, "distance_mi"),
            "costar_vacancy_pct": _manifest_float(prop, "costar_vacancy_pct"),
        }

        print(f"[{i + 1}/{len(rows)}] {name}")

        if not direct_url:
            print("  → no_url (run /pull-new-builds to discover direct site URLs via WebSearch)")
            new_records.append(_null_row(base, "no_url", "none"))
            stats["no_url"] += 1
            continue

        scrape = detect_and_scrape(direct_url, run_date)
        platform = scrape["platform"] or "unknown"

        if scrape["unit_rows"]:
            unit_type_rows = aggregate_to_unit_types(
                scrape["unit_rows"], scrape["specials"], base["total_units"]
            )
            for utr in unit_type_rows:
                new_records.append({**base, **utr, "status": "success", "data_source": platform})

            if not prop.get("scrape_platform"):
                prop["scrape_platform"] = platform
            if not prop.get("first_scraped_date"):
                prop["first_scraped_date"] = run_date

            stats["success"] += 1
            avail = sum(r.get("units_available") or 0 for r in unit_type_rows)
            print(
                f"  → {platform}: {avail} available units "
                f"across {len(unit_type_rows)} unit type(s)"
            )
        else:
            status = (
                platform if platform in ("blocked", "fetch_error") else "parse_error"
            )
            new_records.append(_null_row(base, status, platform))
            stats["failed"] += 1
            errs = "; ".join(scrape["errors"][:2])
            print(f"  → {status}: {errs}")

    write_manifest(rows, manifest_path)

    upsert_to_parquet(new_records, run_date, history_path)

    total = len(rows)
    print(
        f"\nSummary: {stats['success']}/{total} scraped, "
        f"{stats['failed']} failed, {stats['no_url']} no_url"
    )
    print(f"Manifest updated: {manifest_path}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="New-builds vacancy and rent tracker for Austin TX."
    )
    parser.add_argument(
        "--manifest",
        default=str(DEFAULT_MANIFEST),
        help=f"Manifest CSV path (default: {DEFAULT_MANIFEST}).",
    )
    parser.add_argument(
        "--history",
        default=str(DEFAULT_HISTORY),
        help=f"Parquet history path (default: {DEFAULT_HISTORY}).",
    )
    parser.add_argument("--date", help="Scrape date ISO format (default: today).")
    parser.add_argument(
        "--init-from-excel",
        metavar="EXCEL",
        help="Bootstrap manifest from CoStar Excel export (one-time setup).",
    )
    parser.add_argument(
        "--geocode",
        action="store_true",
        help="Geocode manifest rows missing lat/lon via Nominatim (rate-limited 1 req/sec).",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    manifest_path = Path(args.manifest)
    history_path = Path(args.history)
    run_date: str = args.date or dt.date.today().isoformat()

    if args.init_from_excel:
        init_manifest_from_excel(Path(args.init_from_excel), manifest_path)
        return

    if args.geocode:
        geocode_manifest(manifest_path)
        return

    scrape_all(manifest_path, history_path, run_date)


if __name__ == "__main__":
    main()
