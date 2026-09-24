"""Apartments.com source.

Two-stage:
  1. parse_search_html(html) -> list[Card] : pure, fixture-tested.
  2. collect(catchment, run_id) : tries curl-cffi (TLS impersonation) first,
     falls back to Playwright headless Chromium if curl-cffi fails.

HTML format (as of 2026-05-11): Apartments.com migrated from
  <div class="placard" data-listingid="...">
to
  <article class="placard placard-option-{tier} ..." data-listingid="...">

Two placard tiers exist in the current HTML:
  - Platinum/Gold/Silver: large MF properties with bedRentBox fan-out per tier
  - Basic: SFR/smaller listings using the old property-pricing + property-title layout

parse_search_html handles both formats. For platinum/gold/silver cards with
multiple bedRentBox entries, we fan out one Card per bed type (we now have
per-bed pricing directly). For basic cards, we emit one Card as before.

Selectors are centralized so the next site shift has one fix point.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import logging
import re
from dataclasses import dataclass
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
from etl.http_client import chromium_executable_for

logger = logging.getLogger(__name__)

USER_AGENT: Final[str] = (
    "market-study-agent/1.0 (Gainesville TX listings tracker; "
    "maintainers@example.invalid)"
)
PAGE_DELAY_S: Final[float] = 2.5
SEARCH_URL: Final[str] = "https://www.apartments.com/tx/gainesville/"
# curl-cffi impersonation profile: safari17_0 bypasses Akamai TLS fingerprinting
# (chrome1xx variants return 403 on this domain as of 2026-05-11)
CURL_IMPERSONATE: Final[str] = "safari17_0"

# ---------------------------------------------------------------------------
# Regex selectors — updated 2026-05-11 for the new article-based placard HTML
# ---------------------------------------------------------------------------

# Placard root: now <article class="placard placard-option-{tier} ..."> (was <div class="placard">)
# Handles both the old format (kept for fixture compatibility) and the new one.
# Group 3 captures data-streetaddress (used as address fallback for SFR cards).
#
# IMPORTANT — lookahead design:
# We stop at the NEXT placard root OR </body>. The class attribute on a real placard
# root always starts with "placard " (space after "placard") or is exactly "placard"
# before the first space/quote. Nested divs like <div class="placardCarouselImgCount">
# must NOT trigger the lookahead. We achieve this by requiring data-listingid= on the
# lookahead candidate — inner divs never have that attribute.
_PLACARD_RE = re.compile(
    r'<(?:article|div)\s+class="placard[^"]*"\s+data-listingid="([^"]+)"\s+data-url="([^"]+)"'
    r'(?:[^>]*?\bdata-streetaddress="([^"]*)")?[^>]*>'
    r'(.*?)(?=<(?:article|div)[^>]+data-listingid="|</body>)',
    re.DOTALL,
)

# --- New format (platinum/gold/silver tier) ---
# Title: <span class="js-placardTitle title">Name</span>
_TITLE_NEW_RE = re.compile(r'class="js-placardTitle title">([^<]+)<')
# Bed+rent fan-out boxes (one per tier in MF placards)
# bedTextBox: "1 Bed" / "2 Beds" / "3 Beds, 2 Baths, 1,300 sq ft"
# priceTextBox: "$1,150+" (HTML entity &#x2B; or literal +)
_BEDRENTS_RE = re.compile(
    r'<div class="bedTextBox">([^<]+)</div>\s*<div class="priceTextBox">\s*<span>'
    r'(?:&#36;|\$)?([0-9,]+)',
    re.DOTALL,
)
# Address: title attribute on property-address div (new format)
_ADDR_NEW_RE = re.compile(r'class="property-address[^"]*"\s+title="([^"]+)"')

# --- Old/basic format (SFR and basic-tier MF) ---
# Title: <div class="property-title" title="..."><span class="js-placardTitle title">...</span></div>
#   (also matched by _TITLE_NEW_RE above if the span is present)
_TITLE_OLD_RE = re.compile(r'class="property-title"\s+title="([^"]+)"')
# Address: plain text content (old format)
_ADDR_OLD_RE = re.compile(r'class="property-address">([^<]+)<')
# Price: <p class="property-pricing">$1,900</p> (basic/SFR cards)
_PRICE_OLD_RE = re.compile(
    r'class="property-pricing">\s*[^<0-9]*([0-9,]+)(?:\s*-\s*[^<0-9]*([0-9,]+))?'
)
# Beds/baths (old format — still used for basic-tier SFR cards)
_BEDS_OLD_RE = re.compile(
    r'class="property-beds">\s*([0-9.]+)(?:\s*-\s*([0-9.]+))?\s*Beds?', re.IGNORECASE
)
_BATHS_OLD_RE = re.compile(
    r'class="property-baths">\s*([0-9.]+)(?:\s*-\s*([0-9.]+))?\s*Baths?', re.IGNORECASE
)
_SQFT_OLD_RE = re.compile(
    r'class="property-sqft">\s*([0-9,]+)(?:\s*-\s*([0-9,]+))?\s*Sq', re.IGNORECASE
)

# bedTextBox sometimes encodes "3 Beds, 2 Baths, 1,300 sq ft" — parse all three fields
_BEDTEXT_DETAIL_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*Beds?(?:,\s*(\d+(?:\.\d+)?)\s*Baths?)?(?:,\s*([0-9,]+)\s*sq\s*ft)?",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Card:
    listing_id: str
    url: str
    name: str | None
    address_raw: str | None
    beds_min: float | None
    beds_max: float | None
    baths_min: float | None
    baths_max: float | None
    sqft_min: int | None
    sqft_max: int | None
    rent_min: int | None
    rent_max: int | None


def parse_search_html(html: str) -> list[Card]:
    """Parse Apartments.com search results HTML into Card objects.

    Handles two placard formats:
    - New (2026-05-11+): <article class="placard placard-option-{tier}"> with
      bedRentBox entries for MF properties (one per bed type → fan-out).
    - Old/basic: <div class="placard"> or <article class="placard placard-option-basic">
      with property-pricing / property-beds style fields (one card per placard).

    For new-format MF placards with multiple bedRentBox entries we fan out to
    one Card per bed type so downstream analysis sees per-bed rent signals.
    """
    cards: list[Card] = []
    for m in _PLACARD_RE.finditer(html):
        listing_id = m.group(1)
        url = m.group(2)
        street_attr = m.group(3)  # data-streetaddress from tag (may be None/empty)
        body = m.group(4)

        # Name: prefer new span format, fall back to old title= attribute
        title_m = _TITLE_NEW_RE.search(body) or _TITLE_OLD_RE.search(body)
        name = title_m.group(1).strip() if title_m else None

        # Address: prefer new title= attribute on property-address div,
        # then fall back to plain text content, then data-streetaddress tag attr.
        # For basic-tier SFR listings the name IS the address (e.g. "426 N Clements St...").
        addr_m = _ADDR_NEW_RE.search(body) or _ADDR_OLD_RE.search(body)
        if addr_m:
            address_raw = addr_m.group(1).strip()
        elif street_attr:
            address_raw = street_attr  # partial (no city/state/zip), better than None
        elif name and re.match(r"^\d+\s+\w", name):
            # Name looks like a street address — use it (SFR basic tier)
            address_raw = name
        else:
            address_raw = None

        # --- New format: bedRentBox fan-out ---
        bed_rents = _BEDRENTS_RE.findall(body)
        if bed_rents:
            for bed_text, rent_text in bed_rents:
                detail_m = _BEDTEXT_DETAIL_RE.match(bed_text.strip())
                if not detail_m:
                    continue
                beds = float(detail_m.group(1))
                baths = float(detail_m.group(2)) if detail_m.group(2) else None
                sqft = _int_or_none(detail_m.group(3)) if detail_m.group(3) else None
                rent = _int_or_none(rent_text)
                # Append a sub-ID suffix so each fan-out card gets a unique listing_id
                bed_suffix = f"{int(beds)}br"
                cards.append(Card(
                    listing_id=f"{listing_id}-{bed_suffix}",
                    url=url,
                    name=name,
                    address_raw=address_raw,
                    beds_min=beds,
                    beds_max=beds,
                    baths_min=baths,
                    baths_max=baths,
                    sqft_min=sqft,
                    sqft_max=sqft,
                    rent_min=rent,
                    rent_max=rent,
                ))
            continue

        # --- Old/basic format: single card ---
        price_m = _PRICE_OLD_RE.search(body)
        beds_m = _BEDS_OLD_RE.search(body)
        baths_m = _BATHS_OLD_RE.search(body)
        sqft_m = _SQFT_OLD_RE.search(body)
        cards.append(Card(
            listing_id=listing_id,
            url=url,
            name=name,
            address_raw=address_raw,
            beds_min=float(beds_m.group(1)) if beds_m else None,
            beds_max=float(beds_m.group(2) or beds_m.group(1)) if beds_m else None,
            baths_min=float(baths_m.group(1)) if baths_m else None,
            baths_max=float(baths_m.group(2) or baths_m.group(1)) if baths_m else None,
            sqft_min=_int_or_none(sqft_m.group(1)) if sqft_m else None,
            sqft_max=_int_or_none(sqft_m.group(2) or sqft_m.group(1)) if sqft_m else None,
            rent_min=_int_or_none(price_m.group(1)) if price_m else None,
            rent_max=_int_or_none(price_m.group(2) or price_m.group(1)) if price_m else None,
        ))
    return cards


def _int_or_none(s: str | None) -> int | None:
    if s is None:
        return None
    try:
        return int(s.replace(",", ""))
    except ValueError:
        return None


def card_to_observations(
    card: Card,
    run_id: str,
    scraped_at: dt.datetime,
) -> list[RawObservation]:
    """Emit one observation per card.

    For new-format fan-out cards (beds_min == beds_max, per-bed rent known),
    the title is just the property name. For old-format ranged cards where
    per-bed rent is unknown, the range is encoded in the title and the
    conservative (min) values are used.
    """
    sub_id = card.listing_id
    obs_id = hashlib.sha1(f"apartments_com|{sub_id}|{run_id}".encode()).hexdigest()
    is_range = (
        card.beds_min is not None
        and card.beds_max is not None
        and card.beds_min != card.beds_max
    )
    if is_range:
        # Old format: ranged card — encode the range in the title so downstream
        # analysis can see it; use conservative (min) values for numeric fields.
        baths_lo = f"{card.baths_min:g}" if card.baths_min is not None else "?"
        baths_hi = f"{card.baths_max:g}" if card.baths_max is not None else "?"
        range_suffix = (
            f"{card.beds_min:g}-{card.beds_max:g}BR/{baths_lo}-{baths_hi}BA"
        )
        title_with_range = (
            f"{card.name} — {range_suffix}" if card.name else range_suffix
        )
    else:
        title_with_range = card.name
    return [
        RawObservation(
            observation_id=obs_id,
            run_id=run_id,
            source="apartments_com",
            source_listing_id=sub_id,
            url=card.url,
            scraped_at=scraped_at,
            listing_kind="mf",
            address_raw=card.address_raw or "",
            address_normalized=normalize_address(card.address_raw or ""),
            addr_norm_version=ADDR_NORM_VERSION,
            city="Gainesville",
            zip=_zip_from_address(card.address_raw),
            lat=None,
            lon=None,
            beds=card.beds_min,  # exact for fan-out cards; low end for ranged cards
            baths=card.baths_min,
            sqft=card.sqft_min,
            asking_rent=card.rent_min,  # exact for fan-out cards; low end for ranged cards
            concessions_text=None,
            date_posted=None,
            date_available=None,
            title=title_with_range,
            body=None,
            raw_payload_path=None,
        )
    ]


def _zip_from_address(addr: str | None) -> str | None:
    if not addr:
        return None
    m = re.search(r"\b(\d{5})\b", addr)
    return m.group(1) if m else None


def _fetch_via_curl_cffi(url: str) -> str | None:
    """Fetch *url* using curl-cffi TLS impersonation; return HTML or None on failure.

    Apartments.com blocks headless Chromium via Akamai TLS fingerprinting but
    passes safari17_0 impersonation as of 2026-05-11. Chrome variants still 403.
    """
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


def collect_via_curl_cffi(catchment: Catchment, run_id: str) -> CollectionResult:  # noqa: ARG001
    """Collect via curl-cffi TLS impersonation (primary path since 2026-05-11).

    Apartments.com serves its full search HTML including listing data in one
    page request (no JS execution needed) when the TLS fingerprint matches
    a real browser. curl-cffi with safari17_0 satisfies Akamai's first-layer
    filter. We scrape SEARCH_URL directly; no Playwright required.

    Pagination: Gainesville is a small market (~15 listings). If a 'next page'
    link appears in the HTML we follow it via curl-cffi too (up to 5 pages).
    """
    scraped_at = dt.datetime.now(dt.UTC)
    obs_list: list[RawObservation] = []
    pages_scraped = 0
    url: str | None = SEARCH_URL
    seen_urls: set[str] = set()

    while url and pages_scraped < 5:
        if url in seen_urls:
            break
        seen_urls.add(url)
        html = _fetch_via_curl_cffi(url)
        if not html:
            return CollectionResult(
                obs_list,
                CollectionStatus.NETWORK_FAILED,
                {"pages_scraped": pages_scraped, "error": "curl-cffi returned no HTML"},
            )
        pages_scraped += 1
        cards = parse_search_html(html)
        for card in cards:
            obs_list.extend(card_to_observations(card, run_id, scraped_at))
        # Follow pagination: Apartments.com next-page link pattern
        next_m = re.search(r'<a[^>]+class="[^"]*next[^"]*"[^>]+href="([^"]+)"', html)
        url = next_m.group(1) if next_m else None

    if not obs_list:
        return CollectionResult(
            [], CollectionStatus.ZERO_RESULTS_SUSPICIOUS, {"pages_scraped": pages_scraped}
        )
    return CollectionResult(obs_list, CollectionStatus.OK, {"pages_scraped": pages_scraped})


def collect(catchment: Catchment, run_id: str) -> CollectionResult:
    """Collect listings. Tries curl-cffi first; falls back to Playwright.

    Primary path (curl-cffi): TLS impersonation bypasses Akamai bot detection.
    Confirmed working 2026-05-11 with safari17_0 impersonation.

    Fallback (Playwright): kept for defense-in-depth; will fire if Akamai
    tightens detection beyond TLS fingerprint (e.g. browser behaviour checks).
    """
    # --- Primary: curl-cffi ---
    result = collect_via_curl_cffi(catchment, run_id)
    if result.status == CollectionStatus.OK:
        logger.info(
            "apartments.com curl-cffi: %d observations, %d pages",
            len(result.observations),
            result.diagnostics.get("pages_scraped", 0),
        )
        return result
    logger.warning(
        "apartments.com curl-cffi failed (%s), falling back to Playwright", result.status.value
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
            context = browser.new_context(user_agent=USER_AGENT)
            page = context.new_page()
            page.goto(SEARCH_URL, timeout=30000)
            page.wait_for_selector("[data-listingid]", timeout=15000)
            while True:
                pages_scraped += 1
                cards = parse_search_html(page.content())
                if not cards:
                    break
                for card in cards:
                    obs_list.extend(card_to_observations(card, run_id, scraped_at))
                next_btn = page.query_selector("a.next")
                if not next_btn or "disabled" in (next_btn.get_attribute("class") or ""):
                    break
                next_btn.click()
                page.wait_for_timeout(int(PAGE_DELAY_S * 1000))
            browser.close()
    except Exception as e:  # noqa: BLE001
        logger.error("apartments.com Playwright run failed: %s", e)
        return CollectionResult(obs_list, CollectionStatus.NETWORK_FAILED, {"error": str(e)})
    if pages_scraped == 0 or not obs_list:
        return CollectionResult([], CollectionStatus.ZERO_RESULTS_SUSPICIOUS,
                                {"pages_scraped": pages_scraped})
    return CollectionResult(obs_list, CollectionStatus.OK, {"pages_scraped": pages_scraped})


def collect_from_html(htmls: list[str], run_id: str) -> CollectionResult:
    """Test entry point: feed pre-fetched HTML pages instead of using Playwright."""
    scraped_at = dt.datetime.now(dt.UTC)
    obs_list: list[RawObservation] = []
    for html in htmls:
        for card in parse_search_html(html):
            obs_list.extend(card_to_observations(card, run_id, scraped_at))
    if not obs_list:
        return CollectionResult([], CollectionStatus.ZERO_RESULTS_SUSPICIOUS, {})
    return CollectionResult(obs_list, CollectionStatus.OK, {"pages_scraped": len(htmls)})
