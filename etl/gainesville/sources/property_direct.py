"""property_direct: per-property scrapers configured by YAML.

Each site defines CSS selectors for floorplan cards. We use BeautifulSoup
for parsing — it tolerates messy HTML far better than regex on real
property-management vendor sites (Entrata, RentCafe, etc.).

For testing we use collect_from_html(site, html, run_id) which is pure;
the live entry point fetches via Playwright (cookie/JS-friendly).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import logging
import re
import time
from typing import Final

from etl.gainesville.address_normalize import (
    ADDR_NORM_VERSION,
    normalize_address,
)
from etl.gainesville.dataclasses import (
    Catchment,
    CollectionResult,
    CollectionStatus,
    RawObservation,
)
from etl.gainesville.sources.property_direct_config import PropertyDirectSite
from etl.http_client import chromium_executable_for

logger = logging.getLogger(__name__)

USER_AGENT: Final[str] = (
    "market-study-agent/1.0 (Gainesville TX listings tracker; maintainers@example.invalid)"
)
PAGE_DELAY_S: Final[float] = 2.5


def collect_from_html(site: PropertyDirectSite, html: str, run_id: str) -> CollectionResult:
    try:
        from bs4 import BeautifulSoup  # type: ignore[import-untyped]
    except ImportError:
        return CollectionResult([], CollectionStatus.NETWORK_FAILED, {"error": "bs4 not installed"})
    soup = BeautifulSoup(html, "html.parser")
    cards = soup.select(site.selectors["floorplan_card"])
    if not cards:
        return CollectionResult([], CollectionStatus.ZERO_RESULTS_SUSPICIOUS, {"site": site.name})
    scraped_at = dt.datetime.now(dt.UTC)
    obs_list: list[RawObservation] = []
    filtered_out = 0
    for i, card in enumerate(cards):
        name = _text(card, site.selectors["name"])
        if site.name_filter is not None and site.name_filter.lower() not in name.lower():
            filtered_out += 1
            continue
        rent = _int(_text(card, site.selectors["rent"]))
        beds = _float(_text(card, site.selectors["beds"]))
        baths = _float(_text(card, site.selectors["baths"]))
        sqft = _int(_text(card, site.selectors["sqft"]))
        sub_id = f"{site.name}|{i}|{name}"
        obs_id = hashlib.sha1(f"property_direct|{sub_id}|{run_id}".encode()).hexdigest()
        obs_list.append(
            RawObservation(
                observation_id=obs_id,
                run_id=run_id,
                source="property_direct",
                source_listing_id=sub_id,
                url=site.url,
                scraped_at=scraped_at,
                listing_kind="mf",
                address_raw=site.address,
                address_normalized=normalize_address(site.address),
                addr_norm_version=ADDR_NORM_VERSION,
                city="Gainesville",
                zip=_zip_from_address(site.address),
                lat=None,
                lon=None,
                beds=beds,
                baths=baths,
                sqft=sqft,
                asking_rent=rent,
                concessions_text=None,
                date_posted=None,
                date_available=None,
                title=name,
                body=None,
                raw_payload_path=None,
            )
        )
    diagnostics: dict[str, object] = {"site": site.name}
    if filtered_out:
        diagnostics["filtered_out"] = filtered_out
    return CollectionResult(obs_list, CollectionStatus.OK, diagnostics)


def collect(
    catchment: Catchment,
    run_id: str,
    *,
    sites: list[PropertyDirectSite],
) -> CollectionResult:
    try:
        from playwright.sync_api import sync_playwright  # type: ignore[import-untyped]
    except ImportError:
        return CollectionResult(
            [], CollectionStatus.NETWORK_FAILED, {"error": "playwright not installed"}
        )
    obs_list: list[RawObservation] = []
    statuses: list[str] = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                executable_path=chromium_executable_for(p),
            )
            context = browser.new_context(user_agent=USER_AGENT)
            for site in sites:
                page = context.new_page()
                try:
                    page.goto(site.url, timeout=30000)
                    page.wait_for_selector(site.selectors["floorplan_card"], timeout=15000)
                    html = page.content()
                except Exception as e:  # noqa: BLE001
                    logger.warning("property_direct site %s failed: %s", site.name, e)
                    statuses.append(f"{site.name}:fail")
                    page.close()
                    continue
                page.close()
                result = collect_from_html(site, html, run_id)
                obs_list.extend(result.observations)
                statuses.append(f"{site.name}:{result.status.value}")
                time.sleep(PAGE_DELAY_S)
            browser.close()
    except Exception as e:  # noqa: BLE001
        logger.error("property_direct top-level failure: %s", e)
        return CollectionResult(obs_list, CollectionStatus.NETWORK_FAILED, {"error": str(e)})
    if not obs_list:
        return CollectionResult([], CollectionStatus.ZERO_RESULTS_SUSPICIOUS, {"sites": statuses})
    return CollectionResult(obs_list, CollectionStatus.OK, {"sites": statuses})


def _text(card: object, selector: str) -> str:
    if not selector:
        return ""
    el = card.select_one(selector)  # type: ignore[attr-defined]
    return el.get_text(strip=True) if el is not None else ""


def _int(s: str) -> int | None:
    m = re.search(r"\d{1,5}", s.replace(",", ""))
    return int(m.group(0)) if m else None


def _float(s: str) -> float | None:
    m = re.search(r"\d+(?:\.\d+)?", s)
    return float(m.group(0)) if m else None


def _zip_from_address(addr: str) -> str | None:
    m = re.search(r"\b(\d{5})\b", addr)
    return m.group(1) if m else None
