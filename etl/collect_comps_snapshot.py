"""Collect weekly rental comp availability from public websites (best-effort).

This is intended for internal owner/operator monitoring.
The script:
- stores only extracted fields (no raw HTML snapshots),
- records source URLs and as-of timestamps,
- degrades gracefully when sites block automated access (e.g., Cloudflare).

Domain blacklist
----------------
Per the global data-quality rule, certain unverified rent aggregators are
forbidden as comp sources. `BLACKLIST_DOMAINS` enumerates the blocked
hosts. Every URL pulled from YAML config (under `target_comps[*].url` or
`comp_monitoring.comps[*].*_url`) is screened by `_validate_url()` at
config-load time inside `main()`. A blocked URL raises `ValueError`
before any network I/O happens — fail-fast, not fail-late.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import html as htmllib
import json
import os
import re
import signal
import threading
import _thread
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, cast

import yaml

from etl.http_client import chromium_executable_for as _chromium_executable_for
from etl.http_client import fetch_html as _fetch_html
from etl.http_client import safe_fetch as _safe_fetch

# Lazy-imported in main() to avoid circular import (the engine package
# wraps parsers defined later in this file).

USER_AGENT: Final[str] = "market-study-agent/comp-monitor/0.1 (internal)"

# Domains forbidden as comp sources per global data-quality rule. Exact match
# OR proper-subdomain match — see `_validate_url` for the precise semantic.
BLACKLIST_DOMAINS: Final[frozenset[str]] = frozenset({"umovefree.com"})
DIRECT_SCRAPE_DEADLINE_S: Final[int] = 120


def _validate_url(url: str) -> None:
    """Reject URLs whose hostname matches (or is a proper subdomain of) a
    blocked domain.

    Match semantics: exact-match on the hostname OR proper subdomain match
    (``host.endswith("." + blocked)``). This deliberately rejects
    ``apartments.umovefree.com`` and ``api.umovefree.com`` while letting
    ``notumovefree.com`` — a legitimate similar-named domain — pass.
    Substring-only matching would falsely block any host containing the
    blocked string anywhere in its name.

    Raises:
        ValueError: when ``url``'s host equals or is a proper subdomain of
            any entry in ``BLACKLIST_DOMAINS``.
    """
    if not url:
        return  # nothing to validate; no-op on empty input

    from urllib.parse import urlparse

    host = urlparse(url).hostname or ""
    if not host:
        return  # malformed URL; downstream fetch will fail naturally
    for blocked in BLACKLIST_DOMAINS:
        if host == blocked or host.endswith("." + blocked):
            raise ValueError(f"Blocked domain '{blocked}' in URL: {url}")


# YAML keys under `comp_monitoring.comps[*]` that may carry a fetchable URL.
_COMP_URL_KEYS: Final[tuple[str, ...]] = (
    "direct_floorplans_url",
    "direct_availability_url",
    "apartments_com_url",
    "appfolio_listings_url",
    "property_url",
    "direct_site_url",
    "url",
)


def _validate_config_urls(cfg: dict[str, Any]) -> None:
    """Walk a parsed YAML config and `_validate_url()` every comp-source URL.

    Covers two locations:
      1. Top-level `target_comps[*]` (when entries are dicts with a `url` key —
         string entries, used as comp-name labels, are skipped).
      2. `comp_monitoring.comps[*]` URL fields (`direct_floorplans_url`,
         `apartments_com_url`, etc.).

    Raises `ValueError` on the first blocked URL.
    """
    target_comps = cfg.get("target_comps") or []
    if isinstance(target_comps, list):
        for entry in target_comps:
            if isinstance(entry, dict):
                url = entry.get("url")
                if isinstance(url, str) and url:
                    _validate_url(url)

    cm = cfg.get("comp_monitoring") or {}
    for comp in cm.get("comps") or []:
        if not isinstance(comp, dict):
            continue
        for key in _COMP_URL_KEYS:
            url = comp.get(key)
            if isinstance(url, str) and url:
                _validate_url(url)


@dataclass(frozen=True)
class SubjectConfig:
    name: str
    floorplan_summary_csv: str


@dataclass(frozen=True)
class CompConfig:
    name: str
    address: str | None
    direct_floorplans_url: str | None
    direct_availability_url: str | None
    apartments_com_url: str | None
    appfolio_listings_url: str | None
    property_url: str | None = None
    knock_property_id: int | str | None = None
    direct_site_evidence: dict[str, Any] | None = None
    needs_parser_or_manual_review: bool = False
    # Optional total unit count — used by the engine quality gate to set
    # the minimum-floorplans threshold (~ units / 20). Looked up from
    # leaseup_tracking when not declared on the comp itself.
    units_total: int | None = None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Collect public rental comp availability snapshots."
    )
    parser.add_argument(
        "--config", required=True, help="Path to the metro configuration YAML file."
    )
    parser.add_argument("--date", help="ISO date for the snapshot (defaults to today).")
    parser.add_argument(
        "--out-json",
        help=(
            "Override JSON output path (defaults to "
            "data/public/processed/comps/{date}_{metro_slug}_comps_snapshot.json)."
        ),
    )
    parser.add_argument(
        "--out-report",
        help=(
            "Override Markdown output path (defaults to "
            "reports/published/{date}_{metro_slug}_comps.md)."
        ),
    )
    return parser.parse_args()


def fetch_text(url: str, timeout_s: int = 30) -> tuple[int, str]:
    """Fetch URL using the auto-cascade (urllib → curl_cffi → playwright)."""
    return _fetch_html(url, method="auto", timeout_s=timeout_s)


def post_json(url: str, payload: dict[str, Any], timeout_s: int = 30) -> tuple[int, dict[str, Any]]:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "User-Agent": USER_AGENT,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Accept-Language": "en-US,en;q=0.9",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        status = int(getattr(resp, "status", 200))
        raw = resp.read()
    data = json.loads(raw.decode("utf-8", errors="replace"))
    return status, data


def safe_fetch_text(url: str, timeout_s: int = 30) -> dict[str, Any]:
    result = _safe_fetch(url, timeout_s=timeout_s, method="auto")
    # Add body_sha256 for compatibility with downstream callers that check it
    text = result.get("text") or ""
    result["body_sha256"] = hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()
    return result


class _CompScrapeDeadlineExceeded(TimeoutError):
    """Raised when a best-effort comp scrape exceeds the hard deadline."""


def _run_with_deadline(
    timeout_s: float,
    label: str,
    fn,
):
    """Run ``fn`` with a hard real-time deadline on POSIX main-thread calls.

    Network helpers already pass request-level timeouts, but direct-site scrapes
    can still wedge in parser/fallback stacks. Convert those hangs into a normal
    timeout so the collector can salvage partial evidence and continue.
    """
    if timeout_s <= 0 or threading.current_thread() is not threading.main_thread():
        return fn()

    setitimer = getattr(signal, "setitimer", None)
    has_signal_alarm = hasattr(signal, "SIGALRM")
    has_signal_timer = callable(setitimer) and hasattr(signal, "ITIMER_REAL") and has_signal_alarm
    deadline_hit = threading.Event()
    done = threading.Event()

    def _watchdog_interrupt() -> None:
        watchdog_delay = timeout_s
        if has_signal_timer:
            watchdog_delay += min(5.0, max(1.0, timeout_s * 0.05))
        if done.wait(watchdog_delay):
            return
        deadline_hit.set()
        if has_signal_alarm:
            os.kill(os.getpid(), signal.SIGALRM)
        else:
            _thread.interrupt_main()

    def _handle_timeout(_signum, _frame):
        deadline_hit.set()
        raise _CompScrapeDeadlineExceeded(f"{label} exceeded {timeout_s}s")

    watchdog = threading.Thread(target=_watchdog_interrupt, daemon=True)
    watchdog.start()

    previous_handler = None
    previous_timer = (0.0, 0.0)
    if has_signal_alarm:
        previous_handler = signal.getsignal(signal.SIGALRM)
        signal.signal(signal.SIGALRM, _handle_timeout)
    if has_signal_timer:
        previous_timer = setitimer(signal.ITIMER_REAL, timeout_s)
    try:
        return fn()
    except KeyboardInterrupt as exc:
        if deadline_hit.is_set():
            raise _CompScrapeDeadlineExceeded(f"{label} exceeded {timeout_s}s") from exc
        raise
    finally:
        done.set()
        if has_signal_timer:
            setitimer(signal.ITIMER_REAL, 0)
        if has_signal_alarm:
            signal.signal(signal.SIGALRM, previous_handler)
        if has_signal_timer:
            if previous_timer != (0.0, 0.0):
                setitimer(signal.ITIMER_REAL, *previous_timer)


def strip_tags(value: str) -> str:
    value = re.sub(r"<[^>]+>", " ", value)
    value = htmllib.unescape(value)
    return re.sub(r"\s+", " ", value).strip()


def load_subject_floorplan_summary(path: str) -> list[dict[str, Any]]:
    summary_path = Path(path)
    if not summary_path.exists():
        return []

    with summary_path.open("r", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        rows = list(reader)

    normalized: list[dict[str, Any]] = []
    for row in rows:
        try:
            units = int(row.get("Units") or row.get("units") or "0")
            sqft = float(row.get("SqFt") or row.get("sqft") or 0)
            avg = float(
                row.get("AvgMarketRent") or row.get("avg_market_rent") or 0
            )
        except Exception:
            continue
        plan_code = str(row.get("PlanCode") or row.get("floorplan_code") or "").strip()
        bed_type = str(row.get("BedType") or row.get("bed_type") or "").strip()
        if not plan_code or not bed_type:
            continue
        normalized.append(
            {
                "plan_code": plan_code,
                "bed_type": bed_type,
                "units": units,
                "sqft": sqft,
                "avg_market_rent": avg,
            }
        )
    return normalized


def aggregate_subject_plans(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    buckets: dict[str, dict[str, Any]] = {}
    for row in rows:
        plan_code = str(row["plan_code"])
        bucket = buckets.setdefault(
            plan_code,
            {
                "plan_code": plan_code,
                "bed_type": row["bed_type"],
                "units_total": 0,
                "sqft_weighted": 0.0,
                "rent_weighted": 0.0,
            },
        )
        units = int(row["units"])
        bucket["units_total"] += units
        bucket["sqft_weighted"] += float(row["sqft"]) * units
        bucket["rent_weighted"] += float(row["avg_market_rent"]) * units

    out: list[dict[str, Any]] = []
    for bucket in buckets.values():
        units_total = int(bucket["units_total"]) or 1
        out.append(
            {
                "plan_code": bucket["plan_code"],
                "bed_type": bucket["bed_type"],
                "units_total": int(bucket["units_total"]),
                "sqft": round(float(bucket["sqft_weighted"]) / units_total, 1),
                "avg_market_rent": round(float(bucket["rent_weighted"]) / units_total, 2),
            }
        )
    return sorted(out, key=lambda r: (r["bed_type"], r["sqft"], r["plan_code"]))


def pick_best_match(
    subject_plans: list[dict[str, Any]], bed_type: str, sqft: float
) -> dict[str, Any] | None:
    candidates = [p for p in subject_plans if str(p["bed_type"]).strip() == bed_type]
    if not candidates:
        return None
    return min(candidates, key=lambda p: abs(float(p["sqft"]) - sqft))


def parse_resi_rendered_cards(html_text: str) -> dict[str, Any]:
    """Parse Resi/G5 SightMap floorplan cards from Playwright-rendered HTML.

    Fallback parser for sites that no longer expose Vue :floorplans/:units
    attributes but render the data into the DOM via SightMap widgets.
    """
    if "resi__floor_plan" not in html_text and "uk-tile" not in html_text:
        raise ValueError("No Resi rendered card structure found in HTML")

    cards_html = re.split(r'class="uk-tile uk-padding-small', html_text)
    if len(cards_html) < 2:
        raise ValueError("No uk-tile floorplan cards found")

    unit_rows: list[dict[str, Any]] = []
    for card in cards_html[1:]:
        text = re.sub(r"<[^>]+>", "|", card)
        text = re.sub(r"\|+", "|", text)

        bed_match = re.search(r"(\d+)\s*Bed", text)
        bath_match = re.search(r"(\d+(?:\.\d+)?)\s*Bath", text)
        sqft_match = re.search(r"([\d,]+)\s*SF", text)
        price_match = re.search(r"Starting at \$([\d,]+)", text)
        avail_match = re.search(r"(\d+)\s*Unit", text)
        plan_match = re.search(r"\|([A-Z]\d[A-Za-z]?)\|", text)

        if bed_match and price_match:
            beds = int(bed_match.group(1))
            baths = float(bath_match.group(1)) if bath_match else 1.0
            sqft = int(sqft_match.group(1).replace(",", "")) if sqft_match else 0
            rent = int(price_match.group(1).replace(",", ""))
            available = int(avail_match.group(1)) if avail_match else 0
            plan_name = plan_match.group(1) if plan_match else f"{beds}BR/{baths:.0f}BA {sqft}SF"

            unit_rows.append(
                {
                    "unit_number": None,
                    "beds": beds,
                    "baths": baths,
                    "sqft": float(sqft),
                    "rent": float(rent),
                    "available_date": None,
                    "apply_url": None,
                    "floorplan_id": None,
                    "floorplan_name": plan_name,
                }
            )

    if not unit_rows:
        raise ValueError("Resi rendered cards found but no units parsed")

    # Build floorplan summary from the unit rows
    floorplan_rows: list[dict[str, Any]] = []
    for u in unit_rows:
        floorplan_rows.append(
            {
                "floorplan_id": None,
                "floorplan_name": u["floorplan_name"],
                "floorplan_code": u["floorplan_name"],
                "beds": u["beds"],
                "baths": u["baths"],
                "sqft_min": u["sqft"],
                "sqft_max": u["sqft"],
                "rent_min": u["rent"],
                "rent_max": u["rent"],
            }
        )

    specials: list[str] = []
    # Check for specials/concession text
    special_match = re.search(
        r"(?:special|concession|offer|move.?in)[^<]{0,200}",
        html_text,
        re.IGNORECASE,
    )
    if special_match:
        specials.append(strip_tags(special_match.group(0))[:200])

    return {
        "available_units": unit_rows,
        "floorplan_summary": floorplan_rows,
        "specials": specials,
        "parser": "resi_rendered_cards",
    }


def parse_entrata_floorplans(html_text: str) -> dict[str, Any]:
    """Parse Entrata floorplan pages (rendered DOM from Playwright).

    Strategy 1 (preferred): Extract embedded JSON blob with capitalized keys
    (Beds, Baths, MinSqFt, MinRent, MaxRent, AvailableCount).

    Strategy 2 (fallback): Parse "Estimated Monthly Cost $X,XXX" card structure.
    """
    unit_rows: list[dict[str, Any]] = []

    # ── Strategy 1: Entrata JSON blob (capitalized keys) ──
    min_rent_positions = [m.start() for m in re.finditer(r'"MinRent"', html_text)]
    for idx in min_rent_positions:
        candidate_starts = [
            m.start()
            for m in re.finditer(r"\[", html_text[max(0, idx - 5000):idx])
        ]
        window_offset = max(0, idx - 5000)
        for rel_start in reversed(candidate_starts):
            arr_start = window_offset + rel_start
            depth = 0
            arr_end = None
            for i in range(arr_start, min(len(html_text), arr_start + 100000)):
                if html_text[i] == "[":
                    depth += 1
                elif html_text[i] == "]":
                    depth -= 1
                    if depth == 0:
                        arr_end = i + 1
                        break
            if not arr_end:
                continue
            try:
                floorplans = json.loads(html_text[arr_start:arr_end])
            except (json.JSONDecodeError, TypeError):
                continue
            parsed_rows: list[dict[str, Any]] = []
            for fp in floorplans:
                if not isinstance(fp, dict):
                    continue
                beds = int(fp.get("Beds", fp.get("beds", 0)))
                baths = float(fp.get("Baths", fp.get("baths", 1)))
                sqft = int(fp.get("MinSqFt", fp.get("minSqFt", 0)) or 0)
                rent_min = float(fp.get("MinRent", fp.get("minRent", 0)) or 0)
                rent_max = float(fp.get("MaxRent", fp.get("maxRent", rent_min)) or rent_min)
                avail = int(fp.get("AvailableCount", fp.get("availableCount", 0)) or 0)
                name = str(fp.get("Name", fp.get("name", "")))
                if not name:
                    name = f"{beds}BR/{baths:.0f}BA {sqft}SF"

                if rent_min > 0:
                    parsed_rows.append(
                        {
                            "unit_number": None,
                            "beds": beds,
                            "baths": baths,
                            "sqft": float(sqft),
                            "rent": rent_min,
                            "rent_max": rent_max,
                            "available_date": fp.get("AvailableDate"),
                            "available_count": avail,
                            "apply_url": None,
                            "floorplan_id": fp.get("FloorplanId"),
                            "floorplan_name": name,
                        }
                    )
            if parsed_rows:
                unit_rows.extend(parsed_rows)
                break
        if unit_rows:
            break

    if unit_rows:
        specials: list[str] = []
        for pat in (r"(\d+\s*weeks?\s*free[^<.]{0,120})", r"(\d+\s*months?\s*free[^<.]{0,120})"):
            for sm in re.finditer(pat, html_text, re.IGNORECASE):
                snippet = re.sub(r"\s+", " ", sm.group(1)).strip()
                specials.append(snippet)
        return {
            "available_units": unit_rows,
            "floorplan_summary": [],
            "specials": sorted(set(specials))[:5],
            "parser": "entrata",
        }

    # ── Strategy 2: DOM card structure ──
    if "fp-grid-item" not in html_text and "Estimated Monthly Cost" not in html_text:
        raise ValueError("No Entrata pricing structure found")

    try:
        from bs4 import BeautifulSoup
    except ImportError as exc:
        raise ValueError("BeautifulSoup is required for Entrata card parsing") from exc

    soup = BeautifulSoup(html_text, "html.parser")
    for card in soup.select("li.fp-grid-item, div.fp-grid-item"):
        name_el = card.select_one(".fp-name-link") or card.select_one(".fp-name")
        plan_name = name_el.get_text(" ", strip=True) if name_el else ""
        if not plan_name:
            continue

        beds = 0
        baths = 1.0
        bed_bath_el = card.select_one(".details-col.bed-bath .value")
        if bed_bath_el:
            bb_text = bed_bath_el.get_text(" ", strip=True)
            m = re.search(r"(\d+)\s*bd", bb_text, re.IGNORECASE)
            if m:
                beds = int(m.group(1))
            m = re.search(r"(\d+(?:\.\d+)?)\s*ba", bb_text, re.IGNORECASE)
            if m:
                baths = float(m.group(1))

        sqft = 0.0
        sqft_el = card.select_one(".details-col.sq-feet .value")
        if sqft_el:
            sqft = _number_to_float(sqft_el.get_text(" ", strip=True))

        available_units = 0
        avail_el = card.select_one(".available-units")
        if avail_el:
            m = re.search(r"(\d+)", avail_el.get_text(" ", strip=True))
            if m:
                available_units = int(m.group(1))

        rent_min = 0.0
        rent_max = 0.0
        rent_el = card.select_one(".fee-transparency-text") or card.select_one(".details-col.rent .value")
        if rent_el:
            rent_min = _money_to_float(rent_el.get_text(" ", strip=True))
            rent_max = rent_min

        fp_id = None
        action_btn = card.select_one("button.availability[data-url]")
        if action_btn:
            data_url = action_btn.get("data-url") or ""
            m = re.search(r"min_rent=\$?([\d,]+(?:\.\d+)?)", data_url)
            if m:
                rent_min = float(m.group(1).replace(",", ""))
            m = re.search(r"max_rent=\$?([\d,]+(?:\.\d+)?)", data_url)
            if m:
                rent_max = float(m.group(1).replace(",", ""))
            if rent_max <= 0:
                rent_max = rent_min
            fp_match = re.search(r"property_floorplan%5Bid%5D=(\d+)", data_url) or re.search(
                r"property_floorplan\[id\]=(\d+)", data_url
            )
            if fp_match:
                fp_id = fp_match.group(1)

        if rent_min <= 0:
            continue

        unit_rows.append(
            {
                "unit_number": None,
                "beds": beds,
                "baths": baths,
                "sqft": float(sqft),
                "rent": float(rent_min),
                "rent_max": float(rent_max or rent_min),
                "available_date": None,
                "available_count": available_units,
                "apply_url": None,
                "floorplan_id": fp_id,
                "floorplan_name": plan_name,
            }
        )

    if not unit_rows:
        raise ValueError("Entrata structure found but no units parsed")

    return {
        "available_units": unit_rows,
        "floorplan_summary": [],
        "specials": [],
        "parser": "entrata",
    }


def parse_rentcafe_floorplans(html_text: str) -> dict[str, Any]:
    """Parse RentCafe/Yardi floorplan pages.

    Strategy 1 (preferred): Extract the embedded JSON blob with
    "floorplans": [...] that RentCafe injects into the page for its
    Vue/React widget. This is the most reliable approach.

    Strategy 2 (fallback): Parse $/mo pricing patterns from rendered DOM.
    """
    unit_rows: list[dict[str, Any]] = []

    # ── Strategy 1: Embedded JSON blob ──
    idx = html_text.find('"floorplans"')
    if idx > 0:
        arr_start = html_text.find("[", idx)
        if arr_start > 0 and (arr_start - idx) < 50:
            # Count brackets to find matching close
            depth = 0
            arr_end = None
            for i in range(arr_start, min(len(html_text), arr_start + 100000)):
                if html_text[i] == "[":
                    depth += 1
                elif html_text[i] == "]":
                    depth -= 1
                    if depth == 0:
                        arr_end = i + 1
                        break

            if arr_end:
                try:
                    floorplans = json.loads(html_text[arr_start:arr_end])
                    for fp in floorplans:
                        if not isinstance(fp, dict):
                            continue
                        beds = int(fp.get("beds") or 0)
                        baths = float(fp.get("baths") or 1)
                        sqft = int(fp.get("sqft") or 0)
                        low_price = float(fp.get("lowPrice") or fp.get("minRent") or 0)
                        high_price = float(fp.get("highPrice") or fp.get("maxRent") or low_price)
                        name = str(fp.get("name") or f"{beds}BR/{baths:.0f}BA")
                        avail = int(fp.get("availableCount") or fp.get("totalAvailable") or 0)

                        if low_price > 0:
                            unit_rows.append(
                                {
                                    "unit_number": None,
                                    "beds": beds,
                                    "baths": baths,
                                    "sqft": float(sqft),
                                    "rent": low_price,
                                    "rent_max": high_price,
                                    "available_date": fp.get("availableDate"),
                                    "available_count": avail,
                                    "apply_url": None,
                                    "floorplan_id": fp.get("id"),
                                    "floorplan_name": name,
                                }
                            )
                except (json.JSONDecodeError, TypeError):
                    pass  # Fall through to strategy 2

    # ── Strategy 2: DOM price patterns ──
    if not unit_rows:
        for m in re.finditer(
            r"\$([\d,]+)\s*/\s*(?:mo|month)|\$([\d,]+)\s*(?:-|–)\s*\$([\d,]+)",
            html_text,
            re.IGNORECASE,
        ):
            price_str = m.group(1) or m.group(2)
            price = int(price_str.replace(",", ""))
            if price < 400 or price > 10000:
                continue

            start = max(0, m.start() - 1500)
            chunk = html_text[start : m.start()]
            # Strip HTML tags before searching for bed/bath/sqft
            chunk_text = re.sub(r"<[^>]+>", " ", chunk)

            bb_match = re.search(r"(\d+)\s*(?:Bed|BR|bed|bd).*?(\d+)\s*(?:Bath|BA|bath|ba)", chunk_text, re.I)
            sqft_match = re.search(r"([\d,]+)\s*(?:sq\.?\s*ft|SF)", chunk_text, re.I)

            if bb_match:
                beds = int(bb_match.group(1))
                baths = float(bb_match.group(2))
                sqft = int(sqft_match.group(1).replace(",", "")) if sqft_match else 0
                unit_rows.append(
                    {
                        "unit_number": None,
                        "beds": beds,
                        "baths": baths,
                        "sqft": float(sqft),
                        "rent": float(price),
                        "available_date": None,
                        "apply_url": None,
                        "floorplan_id": None,
                        "floorplan_name": f"{beds}BR/{baths:.0f}BA {sqft}SF",
                    }
                )

    if not unit_rows:
        raise ValueError("No RentCafe pricing structure found")

    # Deduplicate
    seen: set[tuple] = set()
    deduped: list[dict[str, Any]] = []
    for u in unit_rows:
        key = (u["beds"], u["baths"], u["sqft"], u["rent"])
        if key not in seen:
            seen.add(key)
            deduped.append(u)

    return {
        "available_units": deduped,
        "floorplan_summary": [],
        "specials": [],
        "parser": "rentcafe",
    }


def parse_swifty_floorplans(html_text: str) -> dict[str, Any]:
    """Parse Swifty-themed floorplan pages (Landing at McCallum, etc).

    Swifty uses .single-floorplan cards with data attributes:
    listing-sqft="NNN" listing-price="NNN" and text content for name/bed/bath.
    Vacancy is in a .floorplan-availability div within the card.
    """
    if "single-floorplan" not in html_text:
        raise ValueError("No Swifty floorplan card structure found")

    unit_rows: list[dict[str, Any]] = []
    for m in re.finditer(r'class="[^"]*single-floorplan[^"]*"([^>]+)>', html_text):
        attrs = m.group(1)
        card_html = html_text[m.end() : m.end() + 5000]

        sqft_match = re.search(r'listing-sqft="\s*(\d+)', attrs)
        price_match = re.search(r'listing-price="(\d+)', attrs)

        # Cards without a price are fully occupied — still include them
        price = int(price_match.group(1)) if price_match else 0

        text = re.sub(r"<[^>]+>", "|", card_html[:1000])
        lines = [l.strip() for l in text.split("|") if l.strip() and len(l.strip()) < 100]

        # Name is first short text line (A1, A2, B1, etc.)
        name = ""
        for line in lines[:5]:
            if re.match(r"^[A-Z]\d", line) and len(line) < 30:
                name = line
                break

        context = " ".join(lines[:8])
        bed_match = re.search(r"(\d+)\s*Bed", context, re.I)
        bath_match = re.search(r"(\d+)\s*Bath", context, re.I)

        # Vacancy from floorplan-availability section
        avail_section = re.search(
            r'class="[^"]*floorplan-availability[^"]*"[^>]*>(.*?)</div>',
            card_html,
            re.DOTALL,
        )
        no_avail_class = re.search(r'class="[^"]*no-availability[^"]*"', card_html)
        vacant_count = 0
        if no_avail_class:
            vacant_count = 0
        elif avail_section:
            avail_text = re.sub(r"<[^>]+>", " ", avail_section.group(1))
            v_match = re.search(r"(\d+)\s*Vacant", avail_text, re.I)
            if v_match:
                vacant_count = int(v_match.group(1))
            elif "No Vacant" in avail_text:
                vacant_count = 0

        unit_rows.append(
            {
                "unit_number": None,
                "beds": int(bed_match.group(1)) if bed_match else 0,
                "baths": float(bath_match.group(1)) if bath_match else 1.0,
                "sqft": float(int(sqft_match.group(1))) if sqft_match else 0.0,
                "rent": float(price) if price > 0 else None,
                "available_date": None,
                "available_count": vacant_count,
                "apply_url": None,
                "floorplan_id": None,
                "floorplan_name": name if name else f"{int(bed_match.group(1)) if bed_match else 0}BR {int(sqft_match.group(1)) if sqft_match else 0}SF",
            }
        )

    if not unit_rows:
        raise ValueError("Swifty cards found but no units parsed")

    # Filter out entries with no rent (fully occupied plans with no listed price)
    # but keep them if they have vacancy data
    unit_rows = [u for u in unit_rows if u["rent"] or u["available_count"] > 0]

    # Deduplicate (Classic vs Renovated are paired — keep max vacancy count)
    by_key: dict[tuple, dict[str, Any]] = {}
    for u in unit_rows:
        key = (u["beds"], u["sqft"], u["rent"])
        if key not in by_key or (u["available_count"] or 0) > (by_key[key]["available_count"] or 0):
            by_key[key] = u

    return {
        "available_units": list(by_key.values()),
        "floorplan_summary": [],
        "specials": [],
        "parser": "swifty",
    }


def parse_jonah_floorplans(html_text: str) -> dict[str, Any]:
    """Parse Jonah Digital (jonahdigital.com) floorplan pages.

    Used by e.g. The James on Highland (Birmingham). Requires Playwright-
    rendered HTML — the unit cards are injected client-side by the Hyly/Jonah
    widget. Each unit is a ``<a data-jd-fp-selector="unit-card" ...>`` with
    inner spans containing bed/bath/sqft/rent.
    """
    if "jd-fp-unit-card" not in html_text:
        raise ValueError("No Jonah Digital floorplan card structure found")

    unit_rows: list[dict[str, Any]] = []

    # Each card starts at <a ... data-jd-fp-selector="unit-card" ...> and
    # ends at the matching </a>. Slice by these markers.
    card_pattern = re.compile(
        r'<a[^>]*data-jd-fp-selector="unit-card"[^>]*>(.*?)</a>',
        re.DOTALL | re.IGNORECASE,
    )
    title_pattern = re.compile(
        r'jd-fp-unit-card__floorplan-title[^>]*>\s*<span>([^<]+)</span>',
        re.IGNORECASE,
    )
    unit_num_pattern = re.compile(
        r'jd-fp-card-info__title[^>]*>\s*(#?[\w\-]+)',
        re.IGNORECASE,
    )
    # "Studio" | "1 bed" | "2 bed"
    bed_pattern = re.compile(r"<span>\s*(Studio|\d+\s*bed[s]?)\s*</span>", re.IGNORECASE)
    bath_pattern = re.compile(r"<span>\s*(\d+(?:\.\d+)?)\s*bath[s]?\s*</span>", re.IGNORECASE)
    sqft_pattern = re.compile(r"<span>\s*([\d,]+)\s*sq\.?\s*ft\.?\s*</span>", re.IGNORECASE)
    rent_pattern = re.compile(r"Starting at\s*\$([\d,]+)", re.IGNORECASE)
    available_pattern = re.compile(r"(Available\s*Now|Available\s*[A-Z][a-z]+\s*\d+)", re.IGNORECASE)

    for m in card_pattern.finditer(html_text):
        card = m.group(1)

        title_m = title_pattern.search(card)
        unit_m = unit_num_pattern.search(card)
        bed_m = bed_pattern.search(card)
        bath_m = bath_pattern.search(card)
        sqft_m = sqft_pattern.search(card)
        rent_m = rent_pattern.search(card)
        avail_m = available_pattern.search(card)

        if not (bed_m and rent_m):
            continue

        bed_raw = bed_m.group(1).lower()
        beds = 0 if "studio" in bed_raw else int(re.search(r"\d+", bed_raw).group(0))
        baths = float(bath_m.group(1)) if bath_m else 1.0
        sqft = float(int(sqft_m.group(1).replace(",", ""))) if sqft_m else 0.0
        rent = float(int(rent_m.group(1).replace(",", "")))
        plan_name = title_m.group(1).strip() if title_m else f"{beds}BR"
        unit_num = unit_m.group(1).strip() if unit_m else None

        unit_rows.append(
            {
                "unit_number": unit_num,
                "beds": beds,
                "baths": baths,
                "sqft": sqft,
                "rent": rent,
                "available_date": avail_m.group(1) if avail_m else None,
                "available_count": 1,
                "apply_url": None,
                "floorplan_id": None,
                "floorplan_name": plan_name,
            }
        )

    if not unit_rows:
        raise ValueError("Jonah Digital cards found but no units parsed")

    # Scrape specials from the same page (concession banners, if any)
    specials: list[str] = []
    for pat in (r"(\d+\s*weeks?\s*free[^<.]{0,80})", r"(\d+\s*months?\s*free[^<.]{0,80})"):
        for sm in re.finditer(pat, html_text, re.IGNORECASE):
            specials.append(sm.group(1).strip())

    return {
        "available_units": unit_rows,
        "floorplan_summary": [],
        "specials": sorted(set(specials)),
        "parser": "jonah",
    }


def parse_sightmap_json(payload: dict[str, Any]) -> dict[str, Any]:
    """Parse a SightMap (sightmap.com) unit inventory JSON payload.

    SightMap is the interactive floorplan map used by Hy.ly / Jonah Digital and
    others. The XHR at ``sightmap.com/app/api/v1/<asset>/sightmaps/<id>`` returns
    the full leasable inventory (per-unit, with price + available_on + beds
    joined from a floor_plans lookup). This is the authoritative count from
    the property's POV — always use it over the DOM widget, which may paginate
    or filter to 'Available Now' only.

    A unit is considered available if it has a truthy ``price`` and
    ``available_on`` (the API omits leased/off-market units entirely — units
    present in the payload are offered for lease).
    """
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict) and isinstance(payload, dict):
        # Some callers pass the already-unwrapped SightMap ``data`` object.
        data = payload
    if not isinstance(data, dict):
        raise ValueError("SightMap payload has no 'data' key")
    units = data.get("units")
    if not isinstance(units, list):
        raise ValueError("SightMap payload has no 'units' list")

    fp_lookup: dict[str, dict[str, Any]] = {}
    for fp in data.get("floor_plans") or []:
        if isinstance(fp, dict) and fp.get("id"):
            fp_lookup[str(fp["id"])] = fp

    unit_rows: list[dict[str, Any]] = []
    for u in units:
        if not isinstance(u, dict):
            continue
        price = u.get("price") or 0
        if not price:
            continue
        fp = fp_lookup.get(str(u.get("floor_plan_id") or ""), {})
        beds = int(fp.get("bedroom_count") or 0)
        baths = float(fp.get("bathroom_count") or 1)
        plan_name_raw = fp.get("filter_label") or fp.get("name") or f"{beds}BR"
        if isinstance(plan_name_raw, str) and plan_name_raw.strip().startswith("{"):
            try:
                plan_name_json = json.loads(plan_name_raw)
                plan_name_raw = plan_name_json.get("name") or plan_name_raw
            except Exception:
                pass
        plan_name = str(plan_name_raw)
        sqft = float(u.get("area") or 0)
        unit_rows.append(
            {
                "unit_number": u.get("unit_number"),
                "beds": beds,
                "baths": baths,
                "sqft": sqft,
                "rent": float(price),
                "available_date": u.get("available_on"),
                "available_count": 1,
                "apply_url": None,
                "floorplan_id": u.get("floor_plan_id"),
                "floorplan_name": plan_name,
            }
        )

    if not unit_rows:
        raise ValueError("SightMap payload had units list but no priced units")

    return {
        "available_units": unit_rows,
        "floorplan_summary": [],
        "specials": [],
        "parser": "sightmap",
    }


def _money_to_float(value: Any) -> float:
    text = str(value or "")
    match = re.search(r"\$?\s*([\d,]+(?:\.\d+)?)", text)
    if not match:
        return 0.0
    return float(match.group(1).replace(",", ""))


def _number_to_float(value: Any) -> float:
    text = str(value or "")
    match = re.search(r"([\d,]+(?:\.\d+)?)", text)
    if not match:
        return 0.0
    return float(match.group(1).replace(",", ""))


def _bed_to_int(value: Any) -> int:
    text = str(value or "").strip().lower()
    if not text or "studio" in text:
        return 0
    match = re.search(r"\d+", text)
    return int(match.group(0)) if match else 0


def parse_knock_units_json(payload: dict[str, Any]) -> dict[str, Any]:
    """Parse Knock Doorway public ``/property/{id}/units`` payloads."""
    units_data = payload.get("units_data") if isinstance(payload, dict) else None
    if not isinstance(units_data, dict):
        raise ValueError("Knock payload has no units_data object")

    layouts: dict[str, dict[str, Any]] = {}
    for layout in units_data.get("layouts") or []:
        if isinstance(layout, dict) and layout.get("id"):
            layouts[str(layout["id"])] = layout

    unit_rows: list[dict[str, Any]] = []
    for unit in units_data.get("units") or []:
        if not isinstance(unit, dict):
            continue
        if unit.get("hidden") or unit.get("leased") or unit.get("reserved"):
            continue
        if unit.get("available") is False:
            continue
        rent = _money_to_float(unit.get("price") or unit.get("displayPrice") or unit.get("knockPrice"))
        if rent <= 0:
            continue
        layout = layouts.get(str(unit.get("layoutId") or "")) or {}
        beds = int(unit.get("bedrooms") if unit.get("bedrooms") is not None else layout.get("bedrooms") or 0)
        baths = float(unit.get("bathrooms") if unit.get("bathrooms") is not None else layout.get("bathrooms") or 1)
        sqft = float(unit.get("area") if unit.get("area") is not None else layout.get("area") or 0)
        unit_rows.append(
            {
                "unit_number": unit.get("name"),
                "beds": beds,
                "baths": baths,
                "sqft": sqft,
                "rent": rent,
                "available_date": unit.get("availableOn"),
                "available_count": 1,
                "apply_url": None,
                "floorplan_id": unit.get("layoutId"),
                "floorplan_name": unit.get("layoutName") or layout.get("name") or f"{beds}BR",
            }
        )

    if not unit_rows:
        raise ValueError("Knock payload had no available priced units")

    return {
        "available_units": unit_rows,
        "floorplan_summary": [],
        "specials": [],
        "parser": "knock_doorway",
    }


def fetch_knock_units_by_property_id(
    property_id: int | str,
    page_url: str,
    timeout_s: int = 30,
) -> dict[str, Any]:
    parsed = urllib.parse.urlparse(page_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/124 Safari/537.36",
        "Accept": "application/json",
        "Origin": origin,
        "Referer": page_url,
    }
    units_url = f"https://doorway-api.knockrentals.com/v1/property/{property_id}/units"
    req = urllib.request.Request(units_url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        payload = json.loads(resp.read().decode("utf-8", errors="replace"))
    return parse_knock_units_json(payload)


def fetch_knock_units_from_page(html_text: str, page_url: str, timeout_s: int = 30) -> dict[str, Any]:
    """Resolve a Knock Doorway property id from public page HTML and fetch units."""
    community_id = None
    public_key = None
    init_match = re.search(
        r"knockDoorway\.init\(\s*['\"]([^'\"]+)['\"]\s*,\s*['\"]community['\"]\s*,\s*['\"]([^'\"]+)['\"]",
        html_text,
    )
    if init_match:
        public_key = init_match.group(1)
        community_id = init_match.group(2)

    parsed = urllib.parse.urlparse(page_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/124 Safari/537.36",
        "Accept": "application/json",
        "Origin": origin,
        "Referer": page_url,
    }

    property_id = None
    if community_id:
        community_url = f"https://doorway-api.knockrentals.com/v1/property/community/{community_id}"
        try:
            req = urllib.request.Request(community_url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                community = json.loads(resp.read().decode("utf-8", errors="replace"))
            prop = community.get("property") or {}
            property_id = prop.get("id") or (prop.get("data") or {}).get("property_id")
        except Exception:
            property_id = None

    if not property_id and public_key:
        profile_url = "https://doorway-api.knockrentals.com/v1/profile?code=w&domain=&refresh=true"
        try:
            req = urllib.request.Request(profile_url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                profile = json.loads(resp.read().decode("utf-8", errors="replace"))
            property_id = (profile.get("profile") or {}).get("property")
        except Exception:
            property_id = None

    if not property_id:
        rendered = fetch_knock_units_via_playwright(page_url, timeout_s=max(timeout_s, 45))
        if rendered is not None:
            return rendered
        raise ValueError("Knock Doorway property id not found")

    return fetch_knock_units_by_property_id(property_id, page_url, timeout_s=timeout_s)


def fetch_knock_units_via_playwright(page_url: str, timeout_s: int = 60) -> dict[str, Any] | None:
    """Render a Knock Doorway page and capture its public units API response."""
    try:
        from playwright.sync_api import sync_playwright  # type: ignore[import]
    except ImportError:
        return None

    captured: list[dict[str, Any]] = []
    ua = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            executable_path=_chromium_executable_for(pw),
        )
        ctx = browser.new_context(user_agent=ua)
        page = ctx.new_page()

        def on_response(resp: Any) -> None:
            if "doorway-api.knockrentals.com/v1/property/" not in resp.url:
                return
            if not resp.url.rstrip("/").endswith("/units"):
                return
            try:
                body = resp.json()
                if isinstance(body, dict):
                    captured.append(body)
            except Exception:
                pass

        page.on("response", on_response)
        try:
            page.goto(page_url, wait_until="networkidle", timeout=timeout_s * 1000)
        except Exception:
            pass
        try:
            page.wait_for_timeout(3000)
        except Exception:
            pass
        browser.close()

    for payload in captured:
        try:
            return parse_knock_units_json(payload)
        except ValueError:
            continue
    return None


def parse_apartments247_floorplans_json(payload: Any) -> dict[str, Any]:
    """Parse Apartments247 public floorplans API payloads."""
    if not isinstance(payload, list):
        raise ValueError("Apartments247 payload is not a list")

    floorplans: list[dict[str, Any]] = []
    unit_rows: list[dict[str, Any]] = []
    for fp in payload:
        if not isinstance(fp, dict):
            continue
        beds = _bed_to_int(fp.get("bed") or fp.get("actual_bed") or 0)
        baths = float(fp.get("bath") or 1)
        sqft = _number_to_float(fp.get("sq_ft") or fp.get("sqft"))
        rent = _money_to_float(fp.get("rent") or fp.get("rent_from") or fp.get("rent_from_text"))
        fp_units = [u for u in (fp.get("units") or []) if isinstance(u, dict)]
        floorplans.append(
            {
                "floorplan_name": fp.get("name") or f"{beds}BR",
                "beds": beds,
                "baths": baths,
                "sqft": sqft,
                "rent_min": rent,
                "rent_max": rent,
                "available_units": len(fp_units),
            }
        )
        for unit in fp_units:
            unit_rent = _money_to_float(unit.get("rent") or rent)
            if unit_rent <= 0:
                continue
            unit_rows.append(
                {
                    "unit_number": unit.get("number"),
                    "beds": _bed_to_int(unit.get("bed") or beds),
                    "baths": float(unit.get("bath") or baths),
                    "sqft": _number_to_float(unit.get("sq_ft") or sqft),
                    "rent": unit_rent,
                    "available_date": unit.get("available_date"),
                    "available_count": 1,
                    "apply_url": unit.get("availability_link"),
                    "floorplan_id": fp.get("id"),
                    "floorplan_name": fp.get("name") or f"{beds}BR",
                }
            )

    if not floorplans and not unit_rows:
        raise ValueError("Apartments247 payload had no floorplans")

    return {
        "platform": "apartments247",
        "floorplans": floorplans,
        "units": unit_rows,
        "specials": [],
    }


def fetch_apartments247_floorplans(html_text: str, page_url: str, timeout_s: int = 30) -> dict[str, Any]:
    key_match = re.search(r"api_key\s*=\s*['\"]([a-f0-9]{32,64})", html_text)
    if not key_match:
        key_match = re.search(r"window\.api_key\s*=\s*['\"]([a-f0-9]{32,64})", html_text)
    if not key_match:
        key_match = re.search(r"api_key[=/]['\"]?([a-f0-9]{32,64})", html_text)
    if not key_match:
        key_match = re.search(r"\b([a-f0-9]{32,64})\b", html_text)
    if not key_match:
        raise ValueError("Apartments247 API key not found")
    parsed = urllib.parse.urlparse(page_url)
    api_url = f"{parsed.scheme}://{parsed.netloc}/api/v3/floorplans/all/?api_key={key_match.group(1)}"
    status, text = fetch_text(api_url, timeout_s=timeout_s)
    if status >= 400:
        raise ValueError(f"Apartments247 API returned status {status}")
    return parse_apartments247_floorplans_json(json.loads(text))


def parse_static_property_floorplans(html_text: str, page_url: str) -> dict[str, Any]:
    """Small deterministic parser for simple brochure pages with visible rents."""
    text = strip_tags(html_text)
    host = urllib.parse.urlparse(page_url).hostname or ""
    if "jubileestudioapts.com" in host:
        rent = _money_to_float(text)
        if rent <= 0:
            raise ValueError("Jubilee page did not expose visible rent")
        specials = []
        for phrase in ("All bills paid", "Immediate Move-In", "Short Term Lease Options", "Furnished"):
            if phrase.lower() in text.lower():
                specials.append(phrase)
        return {
            "platform": "static_property_site",
            "floorplans": [
                {
                    "floorplan_name": "Studio",
                    "beds": 0,
                    "baths": 1.0,
                    "sqft": 0.0,
                    "rent_min": rent,
                    "rent_max": rent,
                    "available_units": 0,
                }
            ],
            "units": [],
            "specials": specials,
        }
    raise ValueError("No static property parser matched")


def _yottareal_api_url(page_url: str | None) -> str | None:
    if not page_url:
        return None
    parsed = urllib.parse.urlparse(page_url)
    qs = urllib.parse.parse_qs(parsed.query)
    if qs.get("dbaid"):
        dbaid = str(qs["dbaid"][0])
        return f"https://residentapis.yottareal.com/api/DBA/GetFloorPlans/{dbaid}"
    m = re.search(r"GetFloorPlans/(\d+)", page_url)
    if m:
        return f"https://residentapis.yottareal.com/api/DBA/GetFloorPlans/{m.group(1)}"
    return None


def _site_specific_salvage(
    *,
    comp: CompConfig,
    effective_direct_url: str,
    base_for_engine: str,
    units_total: int | None,
    run_date: str,
    metro_slug: str,
) -> dict[str, Any] | None:
    """Try known site-family salvage paths after the generic engine comes back empty."""

    site_blob = " ".join(
        part for part in [comp.name or "", effective_direct_url or "", comp.property_url or ""]
        if part
    ).lower()

    if "yottareal" in site_blob or "sunrise bluffs" in (comp.name or "").lower():
        api_url = _yottareal_api_url(effective_direct_url or comp.property_url)
        if not api_url:
            api_url = _yottareal_api_url(comp.property_url)
        if api_url:
            api_fetch = safe_fetch_text(api_url, timeout_s=30)
            if api_fetch["ok"]:
                try:
                    from etl.comp_scraping import normalize_shape
                    from etl.comp_scraping.engines.yottareal import parse_yottareal_floorplans

                    shape_b = normalize_shape(parse_yottareal_floorplans(api_fetch["text"]))
                    if shape_b.get("floorplans") or shape_b.get("units"):
                        return shape_b
                except Exception:
                    pass

    if any(
        token in site_blob
        for token in (
            "entrata.com",
            ".entrata.",
            "/d2/",
            "saratogaridgeaustin.com",
            "rentcafe.com",
            "rent-cafe",
            "yardi.com",
            "logansmillliving.com",
        )
    ):
        from etl.comp_scraping import BUILTIN_ENGINES, LLM_FALLBACK_ENGINE
        from etl.comp_scraping import collect_comp as _collect_comp

        def _browser_fetch_fn(url: str, method: str, timeout: int) -> dict[str, Any]:
            actual_method = "playwright" if method in ("auto", "playwright") else method
            return _safe_fetch(url, timeout_s=timeout, method=actual_method)

        browser_payload = _collect_comp(
            base_url=base_for_engine or (effective_direct_url or comp.property_url or ""),
            units_total=units_total,
            fetch_fn=_browser_fetch_fn,
            registry=BUILTIN_ENGINES,
            fallback=LLM_FALLBACK_ENGINE,
            extraction_queue_dir=f"data/cache/llm_extraction_queue/{run_date}_{metro_slug}",
            comp_name=comp.name,
        )
        if browser_payload.get("floorplans") or browser_payload.get("units"):
            return browser_payload

    return None


def fetch_sightmap_via_playwright(page_url: str, timeout_s: int = 60) -> dict[str, Any] | None:
    """Navigate ``page_url`` in Playwright and capture the SightMap inventory JSON.

    Walks the page, collects every response whose URL matches the SightMap
    inventory endpoint, and returns the largest one (the canonical inventory
    payload — smaller sightmap responses are usually config fetches).
    """
    try:
        from playwright.sync_api import sync_playwright  # type: ignore[import]
    except ImportError:
        return None

    captured: list[tuple[int, dict[str, Any]]] = []
    ua = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            executable_path=_chromium_executable_for(pw),
        )
        ctx = browser.new_context(user_agent=ua)
        page = ctx.new_page()

        def on_response(resp: Any) -> None:
            if "sightmap.com/app/api" not in resp.url:
                return
            if "/sightmaps/" not in resp.url:
                return
            try:
                body = resp.json()
                raw_len = len(resp.body() or b"")
                captured.append((raw_len, body))
            except Exception:
                pass

        page.on("response", on_response)
        try:
            page.goto(page_url, wait_until="networkidle", timeout=timeout_s * 1000)
        except Exception:
            pass  # collect whatever fired before timeout
        # Extra settle — sightmap XHR fires after initial networkidle
        try:
            page.wait_for_timeout(2500)
        except Exception:
            pass
        browser.close()

    if not captured:
        return None
    captured.sort(key=lambda x: x[0], reverse=True)
    return captured[0][1]


def parse_h2_realestate_listings(html_text: str) -> dict[str, Any]:
    """Parse H2 Real Estate portfolio availability page.

    H2 RE lists all available units across their portfolio on a single page.
    Structure (text-based): address → $price+ → / MONTH → N bed → N sqft
    """
    if "/ MONTH" not in html_text and "/MONTH" not in html_text:
        raise ValueError("No H2 Real Estate pricing structure found")

    # Strip to text lines
    text = re.sub(r"<[^>]+>", "\n", html_text)
    lines = [l.strip() for l in text.split("\n") if l.strip()]

    unit_rows: list[dict[str, Any]] = []
    for i, line in enumerate(lines):
        if line.startswith("$") and "+" in line:
            price_str = re.sub(r"[^\d]", "", line)
            if not price_str:
                continue
            price = int(price_str)
            if price < 300 or price > 10000:
                continue

            # Look backward for address
            addr = ""
            for j in range(max(0, i - 5), i):
                if re.search(r"Birmingham|Homewood|Mountain Brook", lines[j], re.I):
                    addr = lines[j]

            # Look forward for bed count, sqft
            beds = 0
            sqft = 0
            for j in range(i, min(len(lines), i + 8)):
                bed_m = re.match(r"^(\d)\s*bed", lines[j], re.I)
                if bed_m:
                    beds = int(bed_m.group(1))
                sqft_m = re.match(r"^(\d[\d,]*)\s*(?:sq|SF)", lines[j], re.I)
                if sqft_m:
                    sqft = int(sqft_m.group(1).replace(",", ""))

            unit_rows.append(
                {
                    "unit_number": None,
                    "beds": beds,
                    "baths": 1.0,
                    "sqft": float(sqft),
                    "rent": float(price),
                    "available_date": None,
                    "apply_url": None,
                    "floorplan_id": None,
                    "floorplan_name": addr if addr else f"{beds}BR {sqft}SF",
                    "address": addr,
                }
            )

    if not unit_rows:
        raise ValueError("H2 RE structure found but no units parsed")

    return {
        "available_units": unit_rows,
        "floorplan_summary": [],
        "specials": [],
        "parser": "h2_realestate",
    }


def parse_appfolio_listings_page(html_text: str) -> dict[str, Any]:
    """Parse AppFolio hosted listings page (reapmgt.appfolio.com/listings).

    Returns unit-level data with bed/bath/sqft/rent from the detail-box format.
    """
    # Each listing card has: RENT, Bed / Bath, Square Feet, Available
    listings = re.findall(
        r'<dt[^>]*>RENT</dt>\s*<dd[^>]*>\$([\d,]+)</dd>.*?'
        r'<dt[^>]*>Bed / Bath</dt>\s*<dd[^>]*>(.*?)</dd>.*?'
        r'<dt[^>]*>Square Feet</dt>\s*<dd[^>]*>([\d,]+)</dd>.*?'
        r'<dt[^>]*>Available</dt>\s*<dd[^>]*>(.*?)</dd>',
        html_text,
        re.DOTALL,
    )

    if not listings:
        raise ValueError("No AppFolio listing cards found")

    unit_rows: list[dict[str, Any]] = []
    for rent_str, bedbath, sqft_str, avail in listings:
        rent = int(rent_str.replace(",", ""))
        sqft = int(sqft_str.replace(",", ""))

        # Parse bed/bath: "1 bd / 1 ba", "1 ba", "2 bd / 2 ba"
        bed_match = re.search(r"(\d+)\s*bd", bedbath)
        bath_match = re.search(r"(\d+)\s*ba", bedbath)
        beds = int(bed_match.group(1)) if bed_match else 0
        baths = float(bath_match.group(1)) if bath_match else 1.0

        unit_rows.append(
            {
                "unit_number": None,
                "beds": beds,
                "baths": baths,
                "sqft": float(sqft),
                "rent": float(rent),
                "available_date": strip_tags(avail).strip(),
                "apply_url": None,
                "floorplan_id": None,
                "floorplan_name": f"{beds}BR/{baths:.0f}BA {sqft}SF",
            }
        )

    return {
        "available_units": unit_rows,
        "floorplan_summary": [],
        "specials": [],
        "parser": "appfolio_listings",
    }


def parse_resi_floorplans_and_units(html_text: str) -> dict[str, Any]:
    idx = html_text.find("<floor-plan-units-modalv2")
    if idx < 0:
        raise ValueError("Resi floorplan modal component not found")
    end = html_text.find(">", idx)
    if end < 0:
        raise ValueError("Resi floorplan modal tag not terminated")
    tag = html_text[idx:end]

    def load_attr(attr: str) -> object | None:
        m = re.search(rf'{re.escape(attr)}="([^"]+)"', tag)
        if not m:
            return None
        return cast(object, json.loads(htmllib.unescape(m.group(1))))

    floorplans_raw = load_attr(":floorplans")
    units_raw = load_attr(":units")
    widget_data_raw = load_attr(":widget-data")
    prop_raw = load_attr(":property")

    floorplans = cast(list[object], floorplans_raw) if isinstance(floorplans_raw, list) else []
    units = cast(list[object], units_raw) if isinstance(units_raw, list) else []
    widget_data = cast(dict[str, Any], widget_data_raw) if isinstance(widget_data_raw, dict) else {}
    prop = cast(dict[str, Any], prop_raw) if isinstance(prop_raw, dict) else {}

    specials: list[str] = []
    for popup in widget_data.get("popups", []):
        title = str(popup.get("title") or "").strip()
        if title:
            specials.append(title)

    unit_rows: list[dict[str, Any]] = []
    for unit in units:
        if not isinstance(unit, dict):
            continue
        if unit.get("unitAvailable") is not True:
            continue
        unit_rows.append(
            {
                "unit_number": unit.get("unitNumber"),
                "beds": int(float(unit.get("unitBedrooms") or 0)),
                "baths": float(unit.get("unitBathrooms") or 0),
                "sqft": float(unit.get("unitInteriorSquareFeet") or 0),
                "rent": float(unit.get("unitPrice") or 0),
                "available_date": unit.get("unitAvailableDate"),
                "apply_url": unit.get("unitApplicationLink"),
                "floorplan_id": unit.get("floorPlanId"),
                "floorplan_name": unit.get("floorPlanName"),
            }
        )

    floorplan_rows: list[dict[str, Any]] = []
    for fp in floorplans:
        if not isinstance(fp, dict):
            continue
        floorplan_rows.append(
            {
                "floorplan_id": fp.get("id"),
                "floorplan_name": fp.get("floorPlanName"),
                "floorplan_code": fp.get("floorPlanCode"),
                "beds": int(float(fp.get("floorPlanBedrooms") or 0)),
                "baths": float(fp.get("floorPlanBathrooms") or 0),
                "sqft_min": float(fp.get("floorPlanInteriorSquareFeet") or 0),
                "sqft_max": float(
                    fp.get("floorPlanInteriorSquareFeetMax")
                    or fp.get("floorPlanInteriorSquareFeet")
                    or 0
                ),
                "rent_min": float(fp.get("floorPlanRentMin") or 0),
                "rent_max": float(fp.get("floorPlanRentMax") or fp.get("floorPlanRentMin") or 0),
                "available_units": int(fp.get("numberUnitsAvailableOrComingSoon") or 0),
            }
        )

    return {
        "platform": "resi-elements",
        "property": {
            "name": prop.get("name"),
            "website": prop.get("website"),
            "phone": prop.get("phone"),
            "address": prop.get("address"),
        },
        "specials": specials,
        "floorplans": floorplan_rows,
        "units": unit_rows,
    }


def parse_bryant_floorplans(html_text: str) -> list[dict[str, Any]]:
    pattern = re.compile(
        r"<h3[^>]*>(?P<name>[^<]+)</h3>.*?fa-bed.*?(?P<beds>\d+)\s*Bed.*?"
        r"fa-bath.*?(?P<baths>\d+)\s*Bath.*?fa-ruler-combined.*?(?P<sqft>\d+)\s*Sq\.?\s*Ft",
        re.IGNORECASE | re.DOTALL,
    )
    out: list[dict[str, Any]] = []
    for m in pattern.finditer(html_text):
        out.append(
            {
                "floorplan_name": strip_tags(m.group("name")),
                "beds": int(m.group("beds")),
                "baths": float(m.group("baths")),
                "sqft": float(m.group("sqft")),
            }
        )
    seen: set[tuple[str, int, float, float]] = set()
    unique: list[dict[str, Any]] = []
    for row in out:
        key = (
            str(row["floorplan_name"]),
            int(row["beds"]),
            float(row["baths"]),
            float(row["sqft"]),
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(row)

    filtered: list[dict[str, Any]] = []
    for row in unique:
        name = str(row.get("floorplan_name") or "").strip().upper()
        if not re.fullmatch(r"[A-Z]\d+[A-Z]?", name):
            continue
        filtered.append(row)
    return filtered


def parse_appfolio_config(html_text: str) -> dict[str, str] | None:
    start = html_text.find("Appfolio.Listing({")
    if start < 0:
        return None
    start += len("Appfolio.Listing({")
    end = html_text.find("});", start)
    if end < 0:
        return None
    body = html_text[start:end]
    host = re.search(r'hostUrl:\s*"([^"]+)"', body)
    group = re.search(r'propertyGroup:\s*"([^"]+)"', body)
    if not host or not group:
        return None
    return {"host_url": host.group(1), "property_group": group.group(1)}


def parse_appfolio_listings(html_text: str) -> list[dict[str, Any]]:
    starts = [m.start() for m in re.finditer(r'<div class="listing-item', html_text)]
    out: list[dict[str, Any]] = []
    for i, start in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else len(html_text)
        chunk = html_text[start:end]

        id_match = re.search(r'id="listing_(\d+)"', chunk)
        listing_id = id_match.group(1) if id_match else None

        title_match = re.search(
            r'<h2 class="listing-item__title[^"]*">\s*<a [^>]*>(.*?)</a>',
            chunk,
            re.DOTALL,
        )
        title = strip_tags(title_match.group(1)) if title_match else None

        address_match = re.search(r'js-listing-address">([^<]+)</span>', chunk)
        address = strip_tags(address_match.group(1)) if address_match else None

        facts: dict[str, str] = {}
        facts_pattern = (
            r'<dt class="detail-box__label">\s*([^<]+)\s*</dt>\s*'
            r'<dd class="detail-box__value[^"]*">\s*([^<]+)\s*</dd>'
        )
        for dt_label, dd_value in re.findall(
            facts_pattern,
            chunk,
            flags=re.DOTALL,
        ):
            facts[strip_tags(dt_label)] = strip_tags(dd_value)

        rent = float(re.sub(r"[^0-9.]", "", facts.get("RENT", "")) or 0)
        sqft = float(re.sub(r"[^0-9.]", "", facts.get("Square Feet", "")) or 0)

        beds = None
        baths = None
        m_bb = re.search(r"(\d+)\s*bd\s*/\s*(\d+)\s*ba", facts.get("Bed / Bath", ""), re.IGNORECASE)
        if m_bb:
            beds = int(m_bb.group(1))
            baths = float(m_bb.group(2))

        if beds is None:
            continue

        out.append(
            {
                "listing_id": listing_id,
                "title": title,
                "address": address,
                "rent": rent,
                "beds": beds,
                "baths": baths,
                "sqft": sqft,
                "available": facts.get("Available"),
                "raw_facts": facts,
            }
        )
    return out


def summarize_bryant_floorplans(
    floorplans: list[dict[str, Any]], units: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    if not floorplans:
        return []

    def best_fp_for_unit(unit: dict[str, Any]) -> dict[str, Any] | None:
        beds = int(unit.get("beds") or 0)
        sqft = float(unit.get("sqft") or 0)
        candidates = [fp for fp in floorplans if int(fp.get("beds") or 0) == beds]
        if not candidates:
            return None
        return min(candidates, key=lambda fp: abs(float(fp.get("sqft") or 0) - sqft))

    by_name: dict[str, list[dict[str, Any]]] = {}
    for unit in units:
        fp = best_fp_for_unit(unit)
        if not fp:
            continue
        name = str(fp.get("floorplan_name") or "").strip()
        if not name:
            continue
        unit = dict(unit)
        unit["floorplan_name"] = name
        by_name.setdefault(name, []).append(unit)

    out: list[dict[str, Any]] = []
    for fp in floorplans:
        name = str(fp.get("floorplan_name") or "").strip()
        grouped = by_name.get(name, [])
        if grouped:
            rents = [float(u.get("rent") or 0) for u in grouped]
            rent_min = min(rents)
            rent_max = max(rents)
            available_units = len(grouped)
        else:
            rent_min = 0.0
            rent_max = 0.0
            available_units = 0

        out.append(
            {
                "floorplan_name": name,
                "beds": int(fp.get("beds") or 0),
                "baths": float(fp.get("baths") or 0),
                "sqft": float(fp.get("sqft") or 0),
                "rent_min": rent_min,
                "rent_max": rent_max,
                "available_units": available_units,
            }
        )
    return out


def summarize_units_by_signature(units: list[dict[str, Any]]) -> list[dict[str, Any]]:
    buckets: dict[tuple[int, float, float], list[dict[str, Any]]] = {}
    for unit in units:
        beds = int(unit.get("beds") or 0)
        baths = float(unit.get("baths") or 0)
        sqft = float(unit.get("sqft") or 0)
        buckets.setdefault((beds, baths, sqft), []).append(unit)

    out: list[dict[str, Any]] = []
    for (beds, baths, sqft), group in sorted(buckets.items(), key=lambda kv: kv[0]):
        rents = [float(u.get("rent") or 0) for u in group]
        positive_rents = [rent for rent in rents if rent > 0]
        rent_min = min(positive_rents) if positive_rents else (min(rents) if rents else 0.0)
        rent_max = max(rents) if rents else 0.0
        out.append(
            {
                "floorplan_name": f"{beds}BR/{int(baths)}BA {sqft:.0f} SF",
                "beds": beds,
                "baths": baths,
                "sqft": sqft,
                "rent_min": rent_min,
                "rent_max": rent_max,
                "available_units": len(group),
            }
        )
    return out


def extract_graphql_query(js_text: str, query_name: str) -> str:
    idx = js_text.find(f"query {query_name}")
    if idx < 0:
        raise ValueError(f"GraphQL query {query_name} not found in bundle")
    start = js_text.rfind('"', 0, idx)
    if start < 0:
        raise ValueError(f"Could not locate starting quote for {query_name}")

    escaped = False
    end: int | None = None
    for i in range(start + 1, len(js_text)):
        ch = js_text[i]
        if escaped:
            escaped = False
            continue
        if ch == "\\":
            escaped = True
            continue
        if ch == '"':
            end = i
            break
    if end is None:
        raise ValueError(f"Could not locate closing quote for {query_name}")

    literal = js_text[start : end + 1]
    try:
        decoded = json.loads(literal)
    except Exception as exc:
        raise ValueError(
            f"Could not decode GraphQL string literal for {query_name}: {exc}"
        ) from exc
    if not isinstance(decoded, str):
        raise ValueError(f"GraphQL literal for {query_name} was not a string")
    return decoded


def parse_g5_floorplans_plus(floorplans_url: str, run_date: str) -> dict[str, Any]:
    # G5 floor-plans-plus config is server-rendered but often blocked by
    # bot detection at the urllib/curl level. Force Playwright to ensure
    # the full page (including config script) is available.
    status, html_text = _fetch_html(floorplans_url, method="playwright", timeout_s=60)
    if status >= 400:
        raise ValueError(f"G5 floorplans page returned status {status}")

    config_match = re.search(
        r'<script[^>]+id="floor-plans-plus-config"[^>]*>(?P<body>.*?)</script>',
        html_text,
        re.DOTALL | re.IGNORECASE,
    )
    if not config_match:
        raise ValueError("floor-plans-plus-config script not found")
    config = json.loads(config_match.group("body").strip())

    base = f"{urllib.parse.urlparse(floorplans_url).scheme}://{urllib.parse.urlparse(floorplans_url).netloc}"
    script_match = re.search(
        r'src="(?P<src>[^"]*floor-plans-plus-[^"]+\.js)"', html_text, re.IGNORECASE
    )
    if not script_match:
        raise ValueError("floor-plans-plus JS bundle not found")
    bundle_url = urllib.parse.urljoin(base, script_match.group("src"))

    _, bundle_js = fetch_text(bundle_url, timeout_s=30)
    query_ac = extract_graphql_query(bundle_js, "ApartmentComplex")
    query_units = extract_graphql_query(bundle_js, "Units")

    graphql_endpoint = str(config["inventoryHost"]).rstrip("/") + "/graphql"
    vars_ac: dict[str, Any] = {
        "locationUrn": config["locationUrn"],
        "moveInDate": run_date,
        "unitsLimit": 500,
    }
    _, ac_payload = post_json(
        graphql_endpoint, {"query": query_ac, "variables": vars_ac}, timeout_s=30
    )
    ac = (ac_payload.get("data") or {}).get("apartmentComplex") or {}
    floorplans = ac.get("floorplans") or []

    floorplan_rows: list[dict[str, Any]] = []
    unit_rows: list[dict[str, Any]] = []
    for fp in floorplans:
        if not isinstance(fp, dict):
            continue
        floorplan_rows.append(
            {
                "floorplan_id": fp.get("id"),
                "floorplan_name": fp.get("name"),
                "beds": int(float(fp.get("beds") or 0)),
                "baths": float(fp.get("baths") or 0),
                "sqft": float(fp.get("sqft") or 0),
                "rent_min": float(fp.get("startingRate") or 0),
                "rent_max": float(fp.get("endingRate") or fp.get("startingRate") or 0),
                "available_units": int(fp.get("totalAvailableUnits") or 0),
                "has_specials": bool(fp.get("hasSpecials") or False),
            }
        )

        if int(fp.get("totalAvailableUnits") or 0) <= 0:
            continue
        fp_id = fp.get("id")
        if fp_id is None:
            continue
        vars_units: dict[str, Any] = {
            "floorplanId": fp_id,
            "locationUrn": config["locationUrn"],
            "limit": 500,
            "moveInDate": run_date,
        }
        _, units_payload = post_json(
            graphql_endpoint, {"query": query_units, "variables": vars_units}, timeout_s=30
        )
        units = (units_payload.get("data") or {}).get("units") or []
        for unit in units:
            if not isinstance(unit, dict):
                continue
            prices = unit.get("prices") or []
            rate = None
            for p in prices:
                if isinstance(p, dict) and p.get("priceType") == "rate":
                    rate = float(p.get("value") or 0)
                    break
            unit_rows.append(
                {
                    "unit_id": unit.get("id"),
                    "unit_number": unit.get("displayName") or unit.get("name"),
                    "availability_date": unit.get("availabilityDate"),
                    "rent": rate,
                    "specials": unit.get("specials") or [],
                    "floorplan_id": fp_id,
                    "floorplan_name": fp.get("name"),
                }
            )

    _backfill_g5_floorplan_rents_from_units(floorplan_rows, unit_rows)

    return {
        "platform": "g5-floor-plans-plus",
        "config": {
            "locationUrn": config.get("locationUrn"),
            "inventoryHost": config.get("inventoryHost"),
            "pricingDisclaimer": ac.get("pricingDisclaimer"),
        },
        "specials": [],
        "floorplans": floorplan_rows,
        "units": unit_rows,
    }


def _backfill_g5_floorplan_rents_from_units(
    floorplan_rows: list[dict[str, Any]],
    unit_rows: list[dict[str, Any]],
) -> None:
    """G5 sometimes omits startingRate but exposes public unit-level rates."""
    rents_by_floorplan: dict[Any, list[float]] = {}
    for unit in unit_rows:
        rent = unit.get("rent")
        floorplan_id = unit.get("floorplan_id")
        if rent in (None, "") or floorplan_id in (None, ""):
            continue
        try:
            rent_value = float(rent)
        except (TypeError, ValueError):
            continue
        if rent_value <= 0:
            continue
        rents_by_floorplan.setdefault(floorplan_id, []).append(rent_value)

    for floorplan in floorplan_rows:
        floorplan_id = floorplan.get("floorplan_id")
        rents = rents_by_floorplan.get(floorplan_id)
        if not rents:
            continue
        rent_min = float(floorplan.get("rent_min") or 0)
        rent_max = float(floorplan.get("rent_max") or 0)
        if rent_min <= 0:
            floorplan["rent_min"] = min(rents)
        if rent_max <= 0:
            floorplan["rent_max"] = max(rents)


# Substrings (lower-cased match) in scrape errors that indicate the failure
# is environmental — browser/network/proxy trouble — rather than a missing
# parser. "executable" covers Playwright's executablePath complaints AND the
# chromium resolver's "No Chromium executable found" RuntimeError.
_INFRA_ERROR_KEYWORDS: Final[tuple[str, ...]] = (
    "playwright",
    "browser",
    "chromium",
    "executable",
    "timeout",
    "timed out",
    "connection refused",
    "connection reset",
    "no such file",
    "dns",
    "name resolution",
    "tls",
    "ssl",
    "proxy",
    "502",
    "503",
    "504",
)


def _flag_infra_failure(meta: dict, comp_record: dict) -> None:
    """Set ``needs_parser_or_manual_review`` when a quality-gate failure looks
    like an infrastructure error (missing browser, network/proxy trouble)
    rather than a missing parser. Mutates both ``meta`` and ``comp_record``
    so operators see the flag in scrape_meta and at the comp level."""
    if meta.get("passes_gate"):
        return
    errors_str = " ".join(str(e) for e in (meta.get("errors") or [])).lower()
    if any(kw in errors_str for kw in _INFRA_ERROR_KEYWORDS):
        comp_record["needs_parser_or_manual_review"] = True
        meta["needs_parser_or_manual_review"] = True


def _is_low_confidence(comp: dict) -> bool:
    """Return True when a comp has low-confidence, LLM-fallback, or
    provenance-less scrape data.

    A published direct payload without ``scrape_meta`` (salvage / static
    parser paths) or without an affirmative ``passes_gate: true`` never
    passed the quality gate and must carry the report's Data Quality
    Notice. The ``not meta.get("passes_gate")`` check deliberately matches
    the loose predicate used by the infra-failure manual-review flag so a
    comp cannot be flagged for review yet publish without a notice.
    """
    direct = comp.get("direct") or {}
    if not direct:
        return False  # nothing published for this comp at all
    meta = direct.get("scrape_meta") or {}
    if not meta:
        return True  # payload published without provenance
    if meta.get("confidence") == "low":
        return True
    if not meta.get("passes_gate"):
        return True  # False, None, or missing
    if "llm" in str(meta.get("engine", "")).lower():
        return True
    return False


def build_report(
    run_date: str,
    metro: str,
    metro_slug: str,
    subjects: list[SubjectConfig],
    snapshot: dict[str, Any],
) -> str:
    lines: list[str] = []
    lines.append(f"# {metro} - Weekly Comp Availability Snapshot")
    lines.append("")
    lines.append(f"**As Of (Run Date):** {run_date}")
    lines.append("")
    lines.append("## Sources & Notes")
    lines.append("")
    lines.append(
        "- Direct property sites are scraped best-effort where structured data is exposed."
    )
    lines.append("- Apartments.com pages are referenced; automated retrieval may be blocked.")
    lines.append("")

    # Quality preamble: inject when any comp has low-confidence or LLM-fallback data
    low_conf_comps = [
        c.get("name") or "unknown"
        for c in snapshot.get("comps", [])
        if _is_low_confidence(c)
    ]
    if low_conf_comps:
        lines.append("> **Data Quality Notice:** Direct scraper data was unavailable or low-confidence")
        lines.append(f"> for {len(low_conf_comps)} propert{'y' if len(low_conf_comps) == 1 else 'ies'}:")
        lines.append(f"> {', '.join(low_conf_comps)}.")
        lines.append(
            "> Floor plan data for these properties is LLM-estimated or was collected"
        )
        lines.append("> without quality-gate verification; treat as low confidence.")
        lines.append("> Verify before use.")
        lines.append("")

    lines.append("## Subject Floorplans (Internal)")
    lines.append("")
    for subject in subjects:
        subject_floorplans = load_subject_floorplan_summary(subject.floorplan_summary_csv)
        agg = aggregate_subject_plans(subject_floorplans)
        lines.append(f"### {subject.name}")
        lines.append("")
        lines.append("| Plan | Bed | Avg Sq Ft | Avg Market Rent | Units |")
        lines.append("| --- | --- | ---: | ---: | ---: |")
        for r in agg:
            plan_code = r["plan_code"]
            bed_type = r["bed_type"]
            sqft = float(r["sqft"])
            avg_rent = float(r["avg_market_rent"])
            units_total = int(r["units_total"])
            lines.append(
                f"| {plan_code} | {bed_type} | {sqft:.0f} | ${avg_rent:.0f} | {units_total} |"
            )
        lines.append("")

    lines.append("## Comps")
    lines.append("")
    for comp in snapshot.get("comps", []):
        lines.append(f"### {comp.get('name')}")
        lines.append("")
        lines.append(f"- **Address:** {comp.get('address') or '-'}")
        lines.append(f"- **Direct URL:** {comp.get('direct_floorplans_url') or '-'}")
        lines.append(f"- **Apartments.com URL:** {comp.get('apartments_com_url') or '-'}")
        evidence = comp.get("direct_site_evidence") or {}
        if isinstance(evidence, dict) and evidence:
            if evidence.get("source_quality"):
                lines.append(f"- **Direct Source Quality:** {evidence.get('source_quality')}")
            if evidence.get("parser"):
                lines.append(f"- **Direct Parser:** {evidence.get('parser')}")
            if evidence.get("findings"):
                lines.append(f"- **Direct Findings:** {evidence.get('findings')}")
        if comp.get("needs_parser_or_manual_review"):
            lines.append("- **Manual Review Flag:** needs_parser_or_manual_review")
        apts = comp.get("apartments_com") or {}
        if apts:
            status = apts.get("status")
            ok = apts.get("ok")
            err = apts.get("error")
            parts = [f"ok={ok}"]
            if status is not None:
                parts.append(f"status={status}")
            if err:
                parts.append(f"error={err}")
            lines.append(f"- **Apartments.com Fetch:** {', '.join([str(p) for p in parts])}")
        if comp.get("errors"):
            lines.append(f"- **Errors:** {', '.join([str(e) for e in comp.get('errors', [])])}")
        lines.append("")

        direct = comp.get("direct") or {}
        floorplans = direct.get("floorplans") or []
        specials = direct.get("specials") or []
        if specials:
            lines.append(f"**Specials (Direct):** {', '.join([str(s) for s in specials])}")
            lines.append("")

        if floorplans:
            lines.append("| Floorplan | Bed | Bath | Sq Ft | Asking Rent | Avail Units |")
            lines.append("| --- | ---: | ---: | ---: | ---: | ---: |")
            for fp in floorplans:
                sqft_cell: str
                if fp.get("sqft_min") is not None and fp.get("sqft_max") is not None:
                    sqft_cell = f"{int(fp.get('sqft_min') or 0)}-{int(fp.get('sqft_max') or 0)}"
                else:
                    sqft_cell = f"{int(float(fp.get('sqft') or 0))}"

                rent_cell = "-"
                if fp.get("rent_min") and float(fp.get("rent_min") or 0) > 0:
                    rent_min = float(fp.get("rent_min") or 0)
                    rent_max = float(fp.get("rent_max") or rent_min)
                    rent_cell = (
                        f"${rent_min:.0f}-${rent_max:.0f}"
                        if rent_max != rent_min
                        else f"${rent_min:.0f}"
                    )

                lines.append(
                    f"| {fp.get('floorplan_name') or '-'} | {fp.get('beds') or 0} | "
                    f"{fp.get('baths') or 0} | {sqft_cell} | {rent_cell} | "
                    f"{fp.get('available_units') or 0} |"
                )
            lines.append("")

        if subjects and floorplans:
            subject_rows = aggregate_subject_plans(
                load_subject_floorplan_summary(subjects[0].floorplan_summary_csv)
            )
            lines.append(f"**Match vs {subjects[0].name} (Nearest Sq Ft, Same Bed Count)**")
            lines.append("")
            lines.append("| Comp Floorplan | Bed | Comp Sq Ft | Comp Asking Rent | Subject Plan | ")
            lines.append("| Subject Avg Sq Ft | Subject Avg Market Rent | Rent Delta |")
            lines.append("| --- | ---: | ---: | ---: | --- | ---: | ---: | ---: |")
            for fp in floorplans:
                bed = int(fp.get("beds") or 0)
                bed_type = (
                    "1BR"
                    if bed == 1
                    else "2BR"
                    if bed == 2
                    else "Studio"
                    if bed == 0
                    else f"{bed}BR"
                )
                comp_sqft = float(fp.get("sqft") or fp.get("sqft_min") or 0)
                comp_rent = float(fp.get("rent_min") or 0)
                if comp_rent <= 0:
                    continue
                match = pick_best_match(subject_rows, bed_type, comp_sqft)
                if not match:
                    continue
                delta = comp_rent - float(match["avg_market_rent"])
                lines.append(
                    f"| {fp.get('floorplan_name') or '-'} | {bed} | {comp_sqft:.0f} | "
                    f"${comp_rent:.0f} | {match['plan_code']} | {match['sqft']:.0f} | "
                    f"${match['avg_market_rent']:.0f} | ${delta:.0f} |"
                )
            lines.append("")

    lines.append(f"*Generated by Market Study Agent comp monitor | Metro slug: `{metro_slug}`*")
    return "\n".join(lines) + "\n"


def main() -> None:
    args = parse_args()
    run_date = args.date or dt.date.today().isoformat()

    cfg_path = Path(args.config)
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    # Fail-fast: refuse to run if any comp URL is on the blacklist.
    _validate_config_urls(cfg)
    metro = str(cfg.get("metro") or "")
    metro_slug = str(
        (cfg.get("notes") or {}).get("metro_slug")
        or metro.lower().replace(",", "").replace(" ", "_")
    )

    cm = cfg.get("comp_monitoring") or {}
    subject_cfgs = [SubjectConfig(**row) for row in (cm.get("subjects") or [])]

    # Build a name → unit-count lookup from leaseup_tracking (where present)
    # so the engine's quality gate can size the min-floorplans threshold
    # without each comp duplicating the unit count.
    leaseup_units: dict[str, int] = {}
    for prop in (cfg.get("leaseup_tracking", {}).get("properties") or []):
        name = prop.get("name")
        units = prop.get("units")
        if name and units:
            try:
                leaseup_units[name] = int(units)
            except (TypeError, ValueError):
                pass

    comp_cfgs = [
        CompConfig(
            name=row.get("name"),
            address=row.get("address"),
            direct_floorplans_url=row.get("direct_floorplans_url"),
            direct_availability_url=row.get("direct_availability_url"),
            apartments_com_url=row.get("apartments_com_url"),
            appfolio_listings_url=row.get("appfolio_listings_url"),
            property_url=row.get("property_url"),
            knock_property_id=row.get("knock_property_id")
            or ((row.get("direct_site_evidence") or {}).get("property_id") if isinstance(row.get("direct_site_evidence"), dict) else None),
            direct_site_evidence=row.get("direct_site_evidence"),
            needs_parser_or_manual_review=bool(row.get("needs_parser_or_manual_review")),
            units_total=row.get("units") or leaseup_units.get(row.get("name")),
        )
        for row in (cm.get("comps") or [])
    ]

    comps_out: list[dict[str, Any]] = []
    for comp in comp_cfgs:
        comp_record: dict[str, Any] = {
            "name": comp.name,
            "address": comp.address,
            "direct_floorplans_url": comp.direct_floorplans_url,
            "direct_availability_url": comp.direct_availability_url,
            "apartments_com_url": comp.apartments_com_url,
            "direct_site_evidence": comp.direct_site_evidence,
            "needs_parser_or_manual_review": comp.needs_parser_or_manual_review,
            "direct": None,
            "apartments_com": None,
            "errors": [],
        }

        # AppFolio listings URL takes priority (structured, reliable, no JS needed)
        if comp.appfolio_listings_url:
            try:
                af_fetch = _run_with_deadline(
                    45,
                    f"{comp.name} AppFolio scrape",
                    lambda: safe_fetch_text(comp.appfolio_listings_url, timeout_s=30),
                )
                if af_fetch["ok"]:
                    try:
                        comp_record["direct"] = parse_appfolio_listings_page(af_fetch["text"])
                    except (ValueError, Exception) as exc:
                        comp_record["errors"].append(f"AppFolio parse error: {exc}")
                else:
                    comp_record["errors"].append(
                        f"AppFolio fetch failed: {af_fetch.get('error')}"
                    )
            except _CompScrapeDeadlineExceeded as exc:
                comp_record["errors"].append(f"AppFolio timeout: {exc}")

        # Resolve a direct URL to try: prefer explicit direct_floorplans_url, else
        # derive one from property_url by appending a /floorplans path. The generic
        # parser cascade below will try multiple parsers on the result.
        effective_direct_url = comp.direct_floorplans_url
        if not effective_direct_url and comp.property_url:
            base = comp.property_url.rstrip("/")
            # First try `{base}/floorplans`; parsers tolerate non-floorplan pages
            # (they raise ValueError and we fall through).
            effective_direct_url = f"{base}/floorplans/"

        if effective_direct_url and comp_record["direct"] is None:
            try:
                def _collect_direct() -> None:
                    nonlocal effective_direct_url
                    direct_fetch = safe_fetch_text(effective_direct_url, timeout_s=30)
                    # If the configured floorplans URL 404s but we have a property
                    # root, retry against the root — engines build their own paths
                    # from a base, so a stale ``direct_floorplans_url`` shouldn't
                    # block extraction.
                    if not direct_fetch["ok"] and comp.property_url:
                        fallback_url = comp.property_url.rstrip("/")
                        if fallback_url != effective_direct_url.rstrip("/"):
                            fallback_fetch = safe_fetch_text(fallback_url, timeout_s=30)
                            if fallback_fetch["ok"]:
                                comp_record["errors"].append(
                                    f"direct_floorplans_url returned "
                                    f"{direct_fetch.get('status')}; falling back to property_url"
                                )
                                effective_direct_url = fallback_url
                                direct_fetch = fallback_fetch
                    units_total = (
                        int(comp.units_total)
                        if getattr(comp, "units_total", None)
                        else None
                    )
                    base_for_engine = (comp.property_url or "").rstrip("/")
                    if not base_for_engine and effective_direct_url:
                        from urllib.parse import urlparse as _urlparse
                        _parsed = _urlparse(effective_direct_url)
                        _path = _parsed.path.rstrip("/")
                        for _suffix in ("/floorplans", "/floor-plans", "/availability"):
                            if _path.endswith(_suffix):
                                _path = _path[: -len(_suffix)]
                                break
                        base_for_engine = (
                            f"{_parsed.scheme}://{_parsed.netloc}{_path}".rstrip("/")
                        )
                    if direct_fetch["ok"]:
                        try:
                            dfu = comp.direct_floorplans_url or ""
                            page_text = direct_fetch.get("text") or ""
                            if comp.knock_property_id:
                                comp_record["direct"] = fetch_knock_units_by_property_id(
                                    comp.knock_property_id,
                                    effective_direct_url,
                                    timeout_s=30,
                                )
                            elif (
                                "doorway.knck.io" in page_text
                                or "knockDoorway.init" in page_text
                            ):
                                comp_record["direct"] = fetch_knock_units_from_page(
                                    page_text,
                                    effective_direct_url,
                                    timeout_s=30,
                                )
                            elif (
                                "apartments247" in page_text.lower()
                                or "api/v3/floorplans" in page_text
                                or "christopherplaceapartmenthomes.com" in (effective_direct_url or "")
                            ):
                                comp_record["direct"] = fetch_apartments247_floorplans(
                                    page_text,
                                    effective_direct_url,
                                    timeout_s=30,
                                )
                            elif "jubileestudioapts.com" in (effective_direct_url or ""):
                                comp_record["direct"] = parse_static_property_floorplans(
                                    page_text,
                                    effective_direct_url,
                                )
                            elif "irtliving.com" in (effective_direct_url or ""):
                                sm_payload = fetch_sightmap_via_playwright(
                                    effective_direct_url, timeout_s=60
                                )
                                if sm_payload:
                                    comp_record["direct"] = parse_sightmap_json(sm_payload)
                                else:
                                    raise ValueError("SightMap XHR capture failed")
                            elif "myashwoodpark.com" in dfu:
                                # Try old Vue-props parser first, fall back to rendered DOM
                                try:
                                    comp_record["direct"] = parse_resi_floorplans_and_units(
                                        direct_fetch["text"]
                                    )
                                except ValueError:
                                    # SightMap widgets need Playwright rendering
                                    pw_fetch = _safe_fetch(
                                        comp.direct_floorplans_url,
                                        timeout_s=60,
                                        method="playwright",
                                    )
                                    if pw_fetch["ok"]:
                                        comp_record["direct"] = parse_resi_rendered_cards(
                                            pw_fetch["text"]
                                        )
                                    else:
                                        raise ValueError("Playwright re-fetch failed")
                            elif (
                                "thejunctionat7760.com" in dfu
                                or "allegiant-carter.com" in dfu
                                or "g5marketingcloud.com" in (direct_fetch.get("text") or "")
                                or "floor-plans-plus" in (direct_fetch.get("text") or "")
                            ):
                                comp_record["direct"] = parse_g5_floorplans_plus(
                                    dfu, run_date
                                )
                            elif "livebryant.com" in dfu:
                                raw_floorplans = parse_bryant_floorplans(direct_fetch["text"])
                                direct: dict[str, Any] = {
                                    "platform": "astro+appfolio",
                                    "floorplans": [],
                                    "units": [],
                                    "specials": [],
                                }
                                if comp.direct_availability_url:
                                    avail_fetch = safe_fetch_text(
                                        comp.direct_availability_url, timeout_s=30
                                    )
                                    if avail_fetch["ok"]:
                                        cfg2 = parse_appfolio_config(avail_fetch["text"])
                                        if cfg2:
                                            listing_url = "https://{host}/listings?{qs}".format(
                                                host=cfg2["host_url"],
                                                qs=urllib.parse.urlencode(
                                                    {"filters[property_list]": cfg2["property_group"]}
                                                ),
                                            )
                                            listing_fetch = safe_fetch_text(listing_url, timeout_s=30)
                                            if listing_fetch["ok"]:
                                                units = parse_appfolio_listings(listing_fetch["text"])
                                                direct["units"] = units
                                                direct["specials"] = sorted(
                                                    {
                                                        (u.get("title") or "").strip()
                                                        for u in units
                                                        if (u.get("title") or "").strip()
                                                    }
                                                )
                                                direct["floorplans"] = (
                                                    summarize_bryant_floorplans(raw_floorplans, units)
                                                    if raw_floorplans
                                                    else summarize_units_by_signature(units)
                                                )
                                            else:
                                                error = listing_fetch.get("error")
                                                comp_record["errors"].append(
                                                    f"AppFolio listings fetch failed: {error}"
                                                )
                                        else:
                                            comp_record["errors"].append("AppFolio embed config not found")
                                    else:
                                        comp_record["errors"].append(
                                            f"Availability fetch failed: {avail_fetch.get('error')}"
                                        )
                                comp_record["direct"] = direct
                            elif (
                                "thejamesonhighland.com" in (effective_direct_url or "")
                                or "jd-fp-unit-card" in (direct_fetch.get("text") or "")
                                or "my.hy.ly" in (direct_fetch.get("text") or "")
                                or "sightmap.com/embed" in (direct_fetch.get("text") or "")
                            ):
                                # Hy.ly / Jonah sites expose their full leasable
                                # inventory via a SightMap XHR — the DOM widget only
                                # paginates to ~24 "Available Now" units, so we MUST
                                # intercept the XHR to get the true count.
                                sm_payload = fetch_sightmap_via_playwright(
                                    effective_direct_url, timeout_s=60
                                )
                                if sm_payload:
                                    try:
                                        comp_record["direct"] = parse_sightmap_json(sm_payload)
                                    except ValueError as exc:
                                        comp_record["errors"].append(
                                            f"SightMap parse failed: {exc}"
                                        )
                                if comp_record["direct"] is None:
                                    # Fall back to DOM-based jonah parser
                                    try:
                                        comp_record["direct"] = parse_jonah_floorplans(
                                            direct_fetch["text"]
                                        )
                                    except ValueError as exc:
                                        comp_record["errors"].append(
                                            f"Jonah DOM parse failed: {exc}"
                                        )
                            else:
                                # Engine-resolver path: lazy import to break the
                                # circular dependency (engines wrap parsers defined
                                # in this file).
                                from etl.comp_scraping import (
                                    BUILTIN_ENGINES,
                                    LLM_FALLBACK_ENGINE,
                                )
                                from etl.comp_scraping import (
                                    collect_comp as _collect_comp,
                                )

                                def _engine_fetch_fn(
                                    url: str, method: str, timeout: int
                                ) -> dict[str, Any]:
                                    if method == "playwright_xhr":
                                        # SightMap-style XHR intercept — engine
                                        # consumes the JSON dict directly.
                                        payload = fetch_sightmap_via_playwright(
                                            url, timeout_s=timeout
                                        )
                                        return {
                                            "ok": payload is not None,
                                            "status": 200 if payload else None,
                                            "text": payload if payload else "",
                                            "error": None
                                            if payload
                                            else "sightmap xhr capture failed",
                                        }
                                    actual_method = (
                                        "playwright" if method == "playwright" else "auto"
                                    )
                                    return _safe_fetch(
                                        url, timeout_s=timeout, method=actual_method
                                    )

                                units_total = (
                                    int(comp.units_total)
                                    if getattr(comp, "units_total", None)
                                    else None
                                )
                                # Engines build per-page URLs by appending paths like
                                # ``/floorplans/`` to ``base_url``. ``effective_direct_url``
                                # often *is* the floorplans page, so derive a property-
                                # root base by trimming the floorplans suffix.
                                base_for_engine = (comp.property_url or "").rstrip("/")
                                if not base_for_engine:
                                    from urllib.parse import urlparse
                                    parsed = urlparse(effective_direct_url)
                                    path = parsed.path.rstrip("/")
                                    for suffix in ("/floorplans", "/floor-plans", "/availability"):
                                        if path.endswith(suffix):
                                            path = path[: -len(suffix)]
                                            break
                                    base_for_engine = (
                                        f"{parsed.scheme}://{parsed.netloc}{path}".rstrip("/")
                                    )
                                engine_payload = _collect_comp(
                                    base_url=base_for_engine,
                                    units_total=units_total,
                                    fetch_fn=_engine_fetch_fn,
                                    registry=BUILTIN_ENGINES,
                                    fallback=LLM_FALLBACK_ENGINE,
                                    extraction_queue_dir=(
                                        f"data/cache/llm_extraction_queue/{run_date}_{metro_slug}"
                                    ),
                                    comp_name=comp.name,
                                )
                                # Always store the engine payload — even when the
                                # quality gate fails we keep specials, pages_fetched,
                                # and errors for downstream auditing. Gate failure
                                # is signalled via ``scrape_meta.passes_gate``.
                                meta = engine_payload.get("scrape_meta") or {}
                                comp_record["direct"] = engine_payload
                                comp_record["errors"].extend(meta.get("errors") or [])
                                # Infrastructure failure detection: flag for manual review
                                _flag_infra_failure(meta, comp_record)
                                if not meta.get("passes_gate") and not engine_payload.get(
                                    "floorplans"
                                ) and not engine_payload.get("specials"):
                                    salvage_payload = _site_specific_salvage(
                                        comp=comp,
                                        effective_direct_url=effective_direct_url,
                                        base_for_engine=base_for_engine,
                                        units_total=units_total,
                                        run_date=run_date,
                                        metro_slug=metro_slug,
                                    )
                                    if salvage_payload:
                                        comp_record["direct"] = salvage_payload
                                    else:
                                        try:
                                            comp_record["direct"] = parse_static_property_floorplans(
                                                direct_fetch["text"],
                                                effective_direct_url,
                                            )
                                        except ValueError:
                                            comp_record["errors"].append(
                                                "No scraper available for direct site"
                                            )
                        except _CompScrapeDeadlineExceeded:
                            raise
                        except Exception as exc:  # pragma: no cover - network/html dependent
                            comp_record["errors"].append(f"Direct parse error: {exc}")
                    else:
                        salvage_payload = _site_specific_salvage(
                            comp=comp,
                            effective_direct_url=effective_direct_url,
                            base_for_engine=base_for_engine,
                            units_total=units_total,
                            run_date=run_date,
                            metro_slug=metro_slug,
                        )
                        if salvage_payload:
                            comp_record["direct"] = salvage_payload
                        else:
                            comp_record["errors"].append(
                                f"Direct fetch failed: {direct_fetch.get('error')}"
                            )

                _run_with_deadline(
                    DIRECT_SCRAPE_DEADLINE_S,
                    f"{comp.name} direct scrape",
                    _collect_direct,
                )
            except _CompScrapeDeadlineExceeded as exc:
                comp_record["errors"].append(f"Direct scrape timeout: {exc}")

        if comp.apartments_com_url:
            try:
                apts_fetch = _run_with_deadline(
                    30,
                    f"{comp.name} apartments.com scrape",
                    lambda: safe_fetch_text(comp.apartments_com_url, timeout_s=20),
                )
                comp_record["apartments_com"] = {
                    "ok": bool(apts_fetch["ok"] and int(apts_fetch.get("status") or 0) < 400),
                    "status": apts_fetch.get("status"),
                    "error": apts_fetch.get("error"),
                    "as_of_utc": apts_fetch.get("as_of_utc"),
                }
            except _CompScrapeDeadlineExceeded as exc:
                comp_record["apartments_com"] = {
                    "ok": False,
                    "status": None,
                    "error": str(exc),
                    "as_of_utc": None,
                }
                comp_record["errors"].append(f"Apartments.com timeout: {exc}")

        # Normalise legacy Shape-A payloads (``available_units`` /
        # ``floorplan_summary``) to Shape B so the snapshot, report, and
        # analyst-grade markdown all see the same per-floorplan rollup.
        if isinstance(comp_record.get("direct"), dict):
            direct = comp_record["direct"]
            if direct.get("available_units") and not direct.get("floorplans"):
                from etl.comp_scraping import normalize_shape

                shape_b = normalize_shape(direct)
                # Preserve the legacy parser tag for traceability.
                shape_b.setdefault("scrape_meta", {})
                shape_b["scrape_meta"].update(
                    {
                        "engine": direct.get("parser") or shape_b.get("platform"),
                        "passes_gate": True,
                        "legacy_path": True,
                    }
                )
                comp_record["direct"] = shape_b

        comps_out.append(comp_record)

    snapshot: dict[str, Any] = {
        "run_date": run_date,
        "metro": metro,
        "metro_slug": metro_slug,
        "comps": comps_out,
    }

    out_json = Path(
        args.out_json or f"data/public/processed/comps/{run_date}_{metro_slug}_comps_snapshot.json"
    )
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(snapshot, indent=2), encoding="utf-8")

    out_report = Path(args.out_report or f"reports/published/{run_date}_{metro_slug}_comps.md")
    out_report.parent.mkdir(parents=True, exist_ok=True)
    out_report.write_text(
        build_report(run_date, metro, metro_slug, subject_cfgs, snapshot), encoding="utf-8"
    )


if __name__ == "__main__":
    main()
