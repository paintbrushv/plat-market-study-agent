"""Zillow rentals scraper.

The risky one. Defensive principles:
  - Soft-block detection: a 'captcha' page or a suspicious-empty result
    page returns a structured status rather than empty observations.
  - Failures here NEVER block other sources from running.

User-agent identifies us honestly. Rate limit: >=2.5s between page loads.

Two collect paths (2026-05-11):
  1. curl-cffi (primary): TLS impersonation bypasses PerimeterX first-layer
     fingerprint filter. Zillow SSR pages embed listing data in
     <script id="__NEXT_DATA__"> JSON — parse_next_data_json() extracts it.
  2. Playwright (fallback): kept for defense-in-depth. Uses the existing
     HTML parser (parse_search_html) which targets <article> cards rendered
     after JS hydration.

ZORI index data continues to flow via the separate `zori` source regardless.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

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
from etl.http_client import chromium_executable_for

logger = logging.getLogger(__name__)

USER_AGENT: Final[str] = (
    "market-study-agent/1.0 (Gainesville TX listings tracker; "
    "maintainers@example.invalid)"
)
PAGE_DELAY_S: Final[float] = 2.5
# curl-cffi impersonation profile: chrome124 and safari17_0 bypass PerimeterX
# TLS fingerprint filter as of 2026-05-11. chrome116 still 403s.
CURL_IMPERSONATE: Final[str] = "chrome124"
# City-name URL: used by curl-cffi path. The bbox URL (?searchQueryState=...) correctly
# filters to the catchment in Playwright (post-JS) but returns 0 listResults in the
# SSR __NEXT_DATA__ JSON. City-name routing returns the full market set (~23 listings
# for Gainesville TX) which we then filter by ZIP code in post-processing.
CURL_CFFI_SEARCH_URL: Final[str] = "https://www.zillow.com/homes/for_rent/Gainesville-TX/"
_NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.DOTALL
)


def detect_soft_block(html: str) -> CollectionStatus | None:
    lowered = html.lower()
    captcha_signals = (
        "captcha-delivery",
        "press &amp; hold",
        "press and hold to confirm",
        "are you a human",
        "captcha-container",
    )
    if any(sig in lowered for sig in captcha_signals):
        return CollectionStatus.CAPTCHA_BLOCKED
    if "no matching results" in lowered or "0 results" in lowered:
        return CollectionStatus.ZERO_RESULTS_SUSPICIOUS
    return None


@dataclass(frozen=True)
class ZillowCard:
    zpid: str
    url: str
    address_raw: str | None
    beds: float | None
    baths: float | None
    sqft: int | None
    rent: int | None
    home_type: str | None


_CARD_RE = re.compile(
    r'<article[^>]*data-test="property-card".*?</article>',
    re.DOTALL,
)
_ZPID_RE = re.compile(r"/(\d+)_zpid/")
_ADDR_RE = re.compile(r"<address>([^<]+)</address>")
_PRICE_RE = re.compile(r'data-test="property-card-price">\s*[$]?([0-9,]+)')
_BEDS_RE = re.compile(r"<b>([0-9.]+)</b>\s*bds", re.IGNORECASE)
_BATHS_RE = re.compile(r"<b>([0-9.]+)</b>\s*ba", re.IGNORECASE)
_SQFT_RE = re.compile(r"<b>([0-9,]+)</b>\s*sqft", re.IGNORECASE)
_HTYPE_RE = re.compile(r'data-test="property-card-home-type">([^<]+)<')


def parse_search_html(html: str) -> list[ZillowCard]:
    cards: list[ZillowCard] = []
    for m in _CARD_RE.finditer(html):
        body = m.group(0)
        zpid_m = _ZPID_RE.search(body)
        addr_m = _ADDR_RE.search(body)
        price_m = _PRICE_RE.search(body)
        beds_m = _BEDS_RE.search(body)
        baths_m = _BATHS_RE.search(body)
        sqft_m = _SQFT_RE.search(body)
        htype_m = _HTYPE_RE.search(body)
        if not zpid_m:
            continue
        url_m = re.search(r'href="([^"]*_zpid/?)"', body)
        cards.append(
            ZillowCard(
                zpid=zpid_m.group(1),
                url=("https://www.zillow.com" + url_m.group(1)) if url_m else "",
                address_raw=addr_m.group(1).strip() if addr_m else None,
                beds=float(beds_m.group(1)) if beds_m else None,
                baths=float(baths_m.group(1)) if baths_m else None,
                sqft=int(sqft_m.group(1).replace(",", "")) if sqft_m else None,
                rent=int(price_m.group(1).replace(",", "")) if price_m else None,
                home_type=htype_m.group(1).strip() if htype_m else None,
            )
        )
    return cards


def card_to_observation(
    card: ZillowCard,
    run_id: str,
    scraped_at: dt.datetime,
) -> RawObservation:
    obs_id = hashlib.sha1(f"zillow|{card.zpid}|{run_id}".encode()).hexdigest()
    kind = _classify_kind(card.home_type)
    return RawObservation(
        observation_id=obs_id,
        run_id=run_id,
        source="zillow",
        source_listing_id=card.zpid,
        url=card.url,
        scraped_at=scraped_at,
        listing_kind=kind,
        address_raw=card.address_raw or "",
        address_normalized=normalize_address(card.address_raw or ""),
        addr_norm_version=ADDR_NORM_VERSION,
        city=None,
        zip=_zip_from_address(card.address_raw),
        lat=None,
        lon=None,
        beds=card.beds,
        baths=card.baths,
        sqft=card.sqft,
        asking_rent=card.rent,
        concessions_text=None,
        date_posted=None,
        date_available=None,
        title=None,
        body=None,
        raw_payload_path=None,
    )


def _classify_kind(home_type: str | None) -> str:
    if not home_type:
        return "unknown"
    ht = home_type.strip().lower()
    # JSON constants from Zillow's __NEXT_DATA__ (hdpData.homeInfo.homeType).
    # These UPPERCASE_UNDERSCORE values appear in the SSR JSON path; the HTML
    # phrase fallback handles legacy parse_search_html / fixture strings.
    if ht in {"single_family", "manufactured"}:
        return "sfr"
    if ht in {"multi_family", "apartment", "condo"}:
        return "mf"
    if ht in {"townhouse", "duplex"}:
        return "duplex"
    if ht in {"lot", "home_type_unknown"}:
        return "unknown"
    # HTML phrase fallback (parse_search_html / hand-crafted fixtures).
    # Check townhouse/duplex BEFORE the generic "house" substring so that
    # "Townhouse for rent" doesn't short-circuit into "sfr".
    if "townhouse" in ht or "duplex" in ht:
        return "duplex"
    if "apartment" in ht or "condo" in ht:
        return "mf"
    if "house" in ht or "single" in ht:
        return "sfr"
    if "manufactured" in ht:
        return "sfr"
    return "unknown"


def _zip_from_address(addr: str | None) -> str | None:
    if not addr:
        return None
    m = re.search(r"\b(\d{5})\b", addr)
    return m.group(1) if m else None


def parse_next_data_json(html: str) -> list[ZillowCard]:
    """Extract listings from Zillow's __NEXT_DATA__ JSON blob.

    Zillow SSR pages embed all search results in a JSON blob inside
    <script id="__NEXT_DATA__">. The path is:
      props.pageProps.searchPageState.cat1.searchResults.listResults[]

    Each entry has: zpid, address, price (formatted), unformattedPrice (int or None),
    beds, baths, area (sqft), detailUrl, rawHomeStatusCd.

    This is more reliable than scraping <article> tags because the JSON is
    present in the initial HTML response (no JS hydration needed).
    """
    nd_m = _NEXT_DATA_RE.search(html)
    if not nd_m:
        return []
    try:
        data: dict[str, Any] = json.loads(nd_m.group(1))
    except json.JSONDecodeError:
        logger.warning("Zillow __NEXT_DATA__ JSON parse failed")
        return []
    try:
        list_results: list[dict[str, Any]] = (
            data["props"]["pageProps"]["searchPageState"]["cat1"]
            ["searchResults"]["listResults"]
        )
    except (KeyError, TypeError):
        logger.warning("Zillow __NEXT_DATA__ missing expected path")
        return []

    cards: list[ZillowCard] = []
    for r in list_results:
        zpid = str(r.get("zpid", ""))
        if not zpid:
            continue
        # unformattedPrice is the integer rent; price is "$2,400/mo" or "Contact Landlord"
        rent_raw = r.get("unformattedPrice")
        rent: int | None = int(rent_raw) if rent_raw is not None else None
        beds_raw = r.get("beds")
        baths_raw = r.get("baths")
        area_raw = r.get("area")
        detail_url = r.get("detailUrl") or ""
        # homeType lives at hdpData.homeInfo.homeType in the SSR JSON (UPPERCASE_UNDERSCORE
        # constants like SINGLE_FAMILY, APARTMENT). The top-level r["homeType"] key is
        # always None in production; fall back to it only for fixture compatibility.
        hdp_home_info: dict[str, Any] = (r.get("hdpData") or {}).get("homeInfo") or {}
        home_type_raw: str | None = hdp_home_info.get("homeType") or r.get("homeType")
        cards.append(
            ZillowCard(
                zpid=zpid,
                url=detail_url if detail_url.startswith("http") else f"https://www.zillow.com{detail_url}",
                address_raw=r.get("address"),
                beds=float(beds_raw) if beds_raw is not None else None,
                baths=float(baths_raw) if baths_raw is not None else None,
                sqft=int(area_raw) if area_raw is not None else None,
                rent=rent,
                home_type=home_type_raw,
            )
        )
    return cards


def _fetch_via_curl_cffi(url: str) -> str | None:
    """Fetch *url* using curl-cffi TLS impersonation; return HTML or None on failure."""
    try:
        from curl_cffi import requests as cffi_requests  # type: ignore[import]
    except ImportError:
        logger.debug("curl_cffi not available, skipping curl-cffi fetch")
        return None
    try:
        resp = cffi_requests.get(url, impersonate=CURL_IMPERSONATE, timeout=30)
        if resp.status_code != 200:
            logger.warning("curl-cffi got HTTP %d for %s", resp.status_code, url)
            return None
        return resp.text
    except Exception as e:  # noqa: BLE001
        logger.warning("curl-cffi fetch failed for %s: %s", url, e)
        return None


def collect_via_curl_cffi(
    catchment: Catchment,
    run_id: str,
) -> CollectionResult:
    """Collect via curl-cffi TLS impersonation (primary path since 2026-05-11).

    Zillow serves its full SSR HTML including listing JSON in one request when
    the TLS fingerprint matches a real Chrome browser. We parse __NEXT_DATA__
    instead of scraping HTML <article> tags (those are JS-hydrated; curl-cffi
    doesn't execute JS).

    URL strategy: CURL_CFFI_SEARCH_URL (city-name form) is used instead of the
    bbox URL (search_url(catchment)) because the bbox URL produces 0 listResults
    in the SSR __NEXT_DATA__ JSON even though it works in post-JS Playwright.
    The city-name URL returns all listings (~23 for Gainesville TX); we filter
    to the catchment ZIP codes after parsing.
    """
    scraped_at = dt.datetime.now(dt.UTC)
    html = _fetch_via_curl_cffi(CURL_CFFI_SEARCH_URL)
    if not html:
        return CollectionResult(
            [], CollectionStatus.NETWORK_FAILED, {"error": "curl-cffi returned no HTML"}
        )
    soft = detect_soft_block(html)
    if soft is not None:
        logger.warning("zillow curl-cffi soft-blocked: %s", soft.value)
        return CollectionResult([], soft, {"pages_scraped": 1})
    cards = parse_next_data_json(html)
    if not cards:
        return CollectionResult(
            [], CollectionStatus.ZERO_RESULTS_SUSPICIOUS, {"pages_scraped": 1}
        )
    # Filter to catchment ZIP codes to avoid pulling listings from neighbouring cities.
    # Cards without a parseable ZIP pass through (ZIP will be derived in normalize step).
    zip_set = set(catchment.zip_codes)
    filtered = [
        c for c in cards
        if not _zip_from_address(c.address_raw) or _zip_from_address(c.address_raw) in zip_set
    ]
    obs_list = [card_to_observation(card, run_id, scraped_at) for card in filtered]
    return CollectionResult(
        obs_list,
        CollectionStatus.OK,
        {"pages_scraped": 1, "total_cards": len(cards), "filtered_cards": len(filtered)},
    )


def search_url(catchment: Catchment) -> str:
    """Build a Zillow rentals search URL filtered to the catchment bbox."""
    south, west, north, east = catchment.bbox
    return (
        "https://www.zillow.com/homes/for_rent/"
        f"?searchQueryState=%7B%22mapBounds%22%3A%7B%22north%22%3A{north}%2C"
        f"%22south%22%3A{south}%2C%22east%22%3A{east}%2C%22west%22%3A{west}%7D"
        "%2C%22filterState%22%3A%7B%22fr%22%3A%7B%22value%22%3Atrue%7D%7D%7D"
    )


def collect(
    catchment: Catchment,
    run_id: str,
    *,
    cookie_state_path: Path | None = None,
    max_pages: int = 5,
) -> CollectionResult:
    """Collect listings. Tries curl-cffi first; falls back to Playwright.

    Primary path (curl-cffi): TLS impersonation bypasses PerimeterX first-layer
    filter. Parses __NEXT_DATA__ JSON (no JS execution needed).
    Confirmed working 2026-05-11 with chrome124 impersonation.

    Fallback (Playwright): kept for defense-in-depth. Uses parse_search_html
    which targets <article> cards rendered after JS hydration.
    """
    # --- Primary: curl-cffi ---
    result = collect_via_curl_cffi(catchment, run_id)
    if result.status == CollectionStatus.OK:
        logger.info(
            "zillow curl-cffi: %d observations, %d pages",
            len(result.observations),
            result.diagnostics.get("pages_scraped", 0),
        )
        return result
    logger.warning(
        "zillow curl-cffi failed (%s), falling back to Playwright", result.status.value
    )

    # --- Fallback: Playwright ---
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return CollectionResult(
            [], CollectionStatus.NETWORK_FAILED, {"error": "playwright not installed"}
        )
    scraped_at = dt.datetime.now(dt.UTC)
    obs_list: list[RawObservation] = []
    pages_scraped = 0
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                executable_path=chromium_executable_for(p),
            )
            ctx_kwargs: dict[str, Any] = {"user_agent": USER_AGENT}
            if cookie_state_path and cookie_state_path.exists():
                ctx_kwargs["storage_state"] = str(cookie_state_path)
            context = browser.new_context(**ctx_kwargs)
            page = context.new_page()
            page.goto(search_url(catchment), timeout=30000)
            page.wait_for_timeout(int(PAGE_DELAY_S * 1000))
            for _ in range(max_pages):
                pages_scraped += 1
                html = page.content()
                soft = detect_soft_block(html)
                if soft is not None:
                    logger.warning("zillow soft-blocked: %s", soft.value)
                    if cookie_state_path:
                        context.storage_state(path=str(cookie_state_path))
                    browser.close()
                    return CollectionResult([], soft, {"pages_scraped": pages_scraped})
                cards = parse_search_html(html)
                for card in cards:
                    obs_list.append(card_to_observation(card, run_id, scraped_at))
                next_btn = page.query_selector('a[title="Next page"]')
                if not next_btn or "disabled" in (next_btn.get_attribute("class") or ""):
                    break
                next_btn.click()
                page.wait_for_timeout(int(PAGE_DELAY_S * 1000))
            if cookie_state_path:
                cookie_state_path.parent.mkdir(parents=True, exist_ok=True)
                context.storage_state(path=str(cookie_state_path))
            browser.close()
    except Exception as e:  # noqa: BLE001
        logger.error("zillow Playwright run failed: %s", e)
        return CollectionResult(
            obs_list, CollectionStatus.NETWORK_FAILED, {"error": str(e)}
        )
    if not obs_list:
        return CollectionResult(
            [], CollectionStatus.ZERO_RESULTS_SUSPICIOUS, {"pages_scraped": pages_scraped}
        )
    return CollectionResult(obs_list, CollectionStatus.OK, {"pages_scraped": pages_scraped})


def collect_from_html(htmls: list[str], run_id: str) -> CollectionResult:
    """Test entry point: feed pre-fetched HTML pages."""
    scraped_at = dt.datetime.now(dt.UTC)
    obs_list: list[RawObservation] = []
    for html in htmls:
        soft = detect_soft_block(html)
        if soft is not None:
            return CollectionResult([], soft, {"pages_scraped": 0})
        for card in parse_search_html(html):
            obs_list.append(card_to_observation(card, run_id, scraped_at))
    if not obs_list:
        return CollectionResult([], CollectionStatus.ZERO_RESULTS_SUSPICIOUS, {})
    return CollectionResult(obs_list, CollectionStatus.OK, {"pages_scraped": len(htmls)})
