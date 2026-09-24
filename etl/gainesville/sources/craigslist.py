"""Craigslist scraper.

Two stages:
  1. HTML search page for the configured region(s) -> RSSItem list
     (one request per keyword, deduplicated by URL).
  2. For each matched item, fetch the post HTML (with delay) and extract
     beds/baths/sqft/rent/lat/lon/zip/body.

Posts expire in 7 days, which is exactly why our cadence is weekly.

Note: Craigslist RSS feeds (?format=rss) return HTTP 403 regardless of
User-Agent as of 2026.  We now scrape the HTML search page instead, which
embeds structured JSON-LD (schema.org ItemList) and a static <ol> fallback
with title, URL, and price.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import re
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final
from urllib.parse import quote_plus

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

logger = logging.getLogger(__name__)

# Craigslist 403s on any non-browser UA for the RSS endpoint (and now for
# some HTML pages too).  Use a realistic Chrome UA + Accept headers.
BROWSER_HEADERS: Final[dict[str, str]] = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/130.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache",
}
REQUEST_DELAY_S: Final[float] = 2.5
# Kept for backwards-compat / unit tests that import it directly.
RSS_NS: Final[dict[str, str]] = {
    "rss": "http://purl.org/rss/1.0/",
    "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
    "dc": "http://purl.org/dc/elements/1.1/",
}


@dataclass(frozen=True)
class RSSItem:
    title: str
    url: str
    posted_at: dt.datetime | None


@dataclass(frozen=True)
class ParsedPost:
    source_listing_id: str | None
    beds: float | None
    baths: float | None
    sqft: int | None
    asking_rent: int | None
    lat: float | None
    lon: float | None
    zip: str | None
    address_raw: str | None
    title: str | None
    body: str | None


def parse_rss(xml_text: str) -> list[RSSItem]:
    items: list[RSSItem] = []
    root = ET.fromstring(xml_text)
    for item in root.iter(f"{{{RSS_NS['rss']}}}item"):
        title_el = item.find("rss:title", RSS_NS)
        link_el = item.find("rss:link", RSS_NS)
        date_el = item.find("dc:date", RSS_NS)
        if title_el is None or link_el is None:
            continue
        posted_at = None
        if date_el is not None and date_el.text:
            try:
                posted_at = dt.datetime.fromisoformat(date_el.text)
            except ValueError:
                posted_at = None
        items.append(RSSItem(
            title=title_el.text or "",
            url=link_el.text or "",
            posted_at=posted_at,
        ))
    return items


def filter_rss_items(items: list[RSSItem], keywords: list[str]) -> list[RSSItem]:
    kw_lower = [k.lower() for k in keywords]
    return [i for i in items if any(k in i.title.lower() for k in kw_lower)]


_BEDS_RE = re.compile(r"(\d+(?:\.\d+)?)\s*BR\b", re.IGNORECASE)
_BATHS_RE = re.compile(r"(\d+(?:\.\d+)?)\s*Ba\b", re.IGNORECASE)
_SQFT_RE = re.compile(r"(\d{3,5})\s*ft<sup>2</sup>|(\d{3,5})\s*sqft", re.IGNORECASE)
_PRICE_RE = re.compile(r'<span class="price">\s*\$([0-9,]+)\s*</span>')
_LATLON_RE = re.compile(r'data-latitude="([0-9.\-]+)"\s+data-longitude="([0-9.\-]+)"')
_ZIP_RE = re.compile(r"\b(\d{5})(?:-\d{4})?\b")
_POSTID_RE = re.compile(r"post id:\s*(\d+)", re.IGNORECASE)
_MAPADDR_RE = re.compile(r'<p class="mapaddress">([^<]+)</p>')
_BODY_RE = re.compile(r'<section id="postingbody">(.*?)</section>', re.DOTALL)
_TITLE_RE = re.compile(r'<span id="titletextonly">([^<]+)</span>')
_JSONLD_BLOCK_RE = re.compile(
    r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>',
    re.DOTALL | re.IGNORECASE,
)
# Matches "426 North Clements St" or "801 Oak Street" — street-number + word(s) + suffix
_BODY_ADDRESS_RE = re.compile(
    r"\b(\d{1,5}\s+[A-Z][A-Za-z]+(?:\s+[A-Z][A-Za-z]+){0,3}\s+"
    r"(?:St|Street|Ave|Avenue|Rd|Road|Blvd|Boulevard|Drive|Dr|Lane|Ln|"
    r"Pkwy|Parkway|Pl|Place|Way|Ct|Court|Cir|Circle))\b\.?",
    re.IGNORECASE,
)
# Strip HTML tags for body plain-text search
_TAG_RE = re.compile(r"<[^>]+>")


def _extract_address_raw(html: str, body_html: str | None) -> str | None:
    """Try multiple sources for address extraction, in priority order:
    1. JSON-LD RealEstateListing schema (streetAddress field)
    2. mapaddress block
    3. Body-text street-number + street-name regex (first match)
    Returns None if no usable address is found.
    """
    # 1. JSON-LD
    for m in _JSONLD_BLOCK_RE.finditer(html):
        try:
            data = json.loads(m.group(1))
        except (json.JSONDecodeError, ValueError):
            continue
        addr = data.get("address") or {}
        street = (addr.get("streetAddress") or "").strip()
        if street:
            locality = (addr.get("addressLocality") or "").strip()
            region = (addr.get("addressRegion") or "").strip()
            postal = (addr.get("postalCode") or "").strip()
            parts = [street]
            if locality:
                parts.append(locality)
            if region:
                parts.append(region)
            if postal:
                parts.append(postal)
            return ", ".join(parts)

    # 2. mapaddress block
    addr_m = _MAPADDR_RE.search(html)
    if addr_m:
        val = addr_m.group(1).strip()
        if val:
            return val

    # 3. Body-text regex — strip HTML tags first
    body_text = _TAG_RE.sub(" ", body_html or "")
    body_m = _BODY_ADDRESS_RE.search(body_text)
    if body_m:
        return re.sub(r"\s+", " ", body_m.group(1).strip().rstrip("."))

    return None


def parse_post_html(html: str) -> ParsedPost:
    beds_m = _BEDS_RE.search(html)
    baths_m = _BATHS_RE.search(html)
    sqft_m = _SQFT_RE.search(html)
    price_m = _PRICE_RE.search(html)
    latlon_m = _LATLON_RE.search(html)
    body_m = _BODY_RE.search(html)
    title_m = _TITLE_RE.search(html)
    postid_m = _POSTID_RE.search(html)

    sqft = None
    if sqft_m:
        sqft = int(sqft_m.group(1) or sqft_m.group(2))
    rent = None
    if price_m:
        rent = int(price_m.group(1).replace(",", ""))
    lat = float(latlon_m.group(1)) if latlon_m else None
    lon = float(latlon_m.group(2)) if latlon_m else None
    body_html = body_m.group(1).strip() if body_m else None

    address_raw = _extract_address_raw(html, body_html) or ""

    zip_match = _ZIP_RE.search(body_html or "") or _ZIP_RE.search(address_raw)
    return ParsedPost(
        source_listing_id=postid_m.group(1) if postid_m else None,
        beds=float(beds_m.group(1)) if beds_m else None,
        baths=float(baths_m.group(1)) if baths_m else None,
        sqft=sqft,
        asking_rent=rent,
        lat=lat,
        lon=lon,
        zip=zip_match.group(1) if zip_match else None,
        address_raw=address_raw,
        title=title_m.group(1) if title_m else None,
        body=(body_html or "")[:2000],
    )


def parse_search_html(html: str) -> list[RSSItem]:
    """Extract listing stubs from a Craigslist HTML search results page.

    Tries the JSON-LD ``ld_searchpage_results`` block first (has lat/lon/beds),
    then falls back to the static ``<ol class="cl-static-search-results">``
    element.  Both paths yield an :class:`RSSItem` with title and URL; the
    individual post fetch (stage 2) fills in the remaining fields.
    """
    items: list[RSSItem] = []
    seen_urls: set[str] = set()

    # --- JSON-LD path (preferred) ---
    ld_m = re.search(
        r'<script[^>]+id="ld_searchpage_results"[^>]*>(.*?)</script>',
        html,
        re.DOTALL,
    )
    if ld_m:
        try:
            data = json.loads(ld_m.group(1))
            for entry in data.get("itemListElement", []):
                item = entry.get("item", {})
                name = (item.get("name") or "").strip()
                if not name:
                    continue
                # URL comes from static HTML; match by position if possible.
                # We collect URLs separately below and zip them in.
                items.append(RSSItem(title=name, url="", posted_at=None))
        except (json.JSONDecodeError, AttributeError):
            pass

    # --- Static HTML fallback / URL extraction ---
    static_entries = re.findall(
        r'<li class="cl-static-search-result"[^>]*>.*?'
        r'<a href="(https://[^"]+/apa/d/[^"]+)"[^>]*>.*?'
        r'<div class="title">([^<]+)</div>',
        html,
        re.DOTALL,
    )
    static_map: dict[str, str] = {}  # title -> url
    for url, title in static_entries:
        static_map[title.strip()] = url.strip()

    # Attach URLs to JSON-LD items; fall back to static-only if JSON-LD absent.
    if items:
        enriched: list[RSSItem] = []
        for it in items:
            url = static_map.get(it.title, "")
            if url and url not in seen_urls:
                seen_urls.add(url)
                enriched.append(RSSItem(title=it.title, url=url, posted_at=None))
        items = enriched
    else:
        for title, url in static_map.items():
            if url not in seen_urls:
                seen_urls.add(url)
                items.append(RSSItem(title=title, url=url, posted_at=None))

    return items


def fetch(url: str, timeout: int = 20) -> str:
    req = urllib.request.Request(url, headers=BROWSER_HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        return resp.read().decode("utf-8", errors="replace")


def collect(
    catchment: Catchment,
    run_id: str,
    *,
    regions: list[str],
    keywords: list[str],
    fetch_fn: Callable[[str], str] = fetch,
    delay_s: float = REQUEST_DELAY_S,
) -> CollectionResult:
    obs_list: list[RawObservation] = []
    scraped_at = dt.datetime.now(dt.UTC)
    field_hits: dict[str, int] = {"asking_rent": 0, "beds": 0, "baths": 0, "sqft": 0, "lat": 0}
    total_posts = 0
    seen_post_urls: set[str] = set()

    for region in regions:
        # Craigslist RSS (?format=rss) returns 403 regardless of User-Agent.
        # Scrape one HTML search page per keyword and deduplicate by URL.
        items: list[RSSItem] = []
        for keyword in keywords:
            search_url = (
                f"https://{region}.craigslist.org/search/apa"
                f"?query={quote_plus(keyword)}"
            )
            try:
                html = fetch_fn(search_url)
            except (urllib.error.URLError, TimeoutError) as e:
                logger.error("Search fetch failed for %s / %s: %s", region, keyword, e)
                return CollectionResult(
                    [], CollectionStatus.NETWORK_FAILED, {"region": region, "error": str(e)}
                )
            for it in parse_search_html(html):
                if it.url and it.url not in seen_post_urls:
                    seen_post_urls.add(it.url)
                    items.append(it)
            time.sleep(delay_s)

        for item in items:
            time.sleep(delay_s)
            try:
                html = fetch_fn(item.url)
            except (urllib.error.URLError, TimeoutError) as e:
                logger.warning("post fetch failed for %s: %s", item.url, e)
                continue
            post = parse_post_html(html)
            total_posts += 1
            if post.asking_rent:
                field_hits["asking_rent"] += 1
            if post.beds is not None:
                field_hits["beds"] += 1
            if post.baths is not None:
                field_hits["baths"] += 1
            if post.sqft:
                field_hits["sqft"] += 1
            if post.lat is not None:
                field_hits["lat"] += 1
            kind = _classify_kind(post)
            obs = _to_observation(item, post, scraped_at, run_id, kind)
            obs_list.append(obs)
    diagnostics: dict[str, object] = {
        "regions": regions,
        "total_posts": total_posts,
        "field_coverage": {
            k: (v / total_posts if total_posts else 0.0) for k, v in field_hits.items()
        },
    }
    return CollectionResult(obs_list, CollectionStatus.OK, diagnostics)


def _classify_kind(post: ParsedPost) -> str:
    title_lower = (post.title or "").lower()
    body_lower = (post.body or "").lower()
    if "duplex" in title_lower or "duplex" in body_lower:
        return "duplex"
    if "fourplex" in title_lower or "4-plex" in title_lower:
        return "fourplex"
    if any(w in title_lower for w in ["apartment", "apt", "complex", " unit "]):
        return "mf"
    return "sfr"


def _to_observation(
    item: RSSItem,
    post: ParsedPost,
    scraped_at: dt.datetime,
    run_id: str,
    kind: str,
) -> RawObservation:
    sid = post.source_listing_id or hashlib.sha1(item.url.encode("utf-8")).hexdigest()[:12]
    obs_id = hashlib.sha1(f"craigslist|{sid}|{run_id}".encode()).hexdigest()
    addr_raw = post.address_raw or ""
    return RawObservation(
        observation_id=obs_id,
        run_id=run_id,
        source="craigslist",
        source_listing_id=sid,
        url=item.url,
        scraped_at=scraped_at,
        listing_kind=kind,
        address_raw=addr_raw,
        address_normalized=normalize_address(addr_raw),
        addr_norm_version=ADDR_NORM_VERSION,
        city="Gainesville" if post.zip == "76240" else None,
        zip=post.zip,
        lat=post.lat,
        lon=post.lon,
        beds=post.beds,
        baths=post.baths,
        sqft=post.sqft,
        asking_rent=post.asking_rent,
        concessions_text=None,
        date_posted=item.posted_at.date() if item.posted_at else None,
        date_available=None,
        title=post.title or item.title,
        body=post.body,
        raw_payload_path=None,
    )
