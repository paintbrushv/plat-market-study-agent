"""Dataclasses for the Gainesville listings tracker.

These types are passed across module boundaries (sources -> orchestrator ->
dedup). Keep them frozen and serializable — no behavior, just data.
"""

from __future__ import annotations

import datetime as dt
import enum
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Catchment:
    """Geographic scope for a collection run."""

    county_fips: str
    zip_codes: tuple[str, ...]
    bbox: tuple[float, float, float, float]  # (south, west, north, east)
    city_limits_geojson_path: str | None


@dataclass(frozen=True)
class RawObservation:
    """One advertised listing as observed by one source on one scrape.

    Mirrors the raw_observations DuckDB table exactly. Append-only —
    never mutated after creation.
    """

    observation_id: str
    run_id: str
    source: str
    source_listing_id: str
    url: str
    scraped_at: dt.datetime  # UTC
    listing_kind: str  # 'mf' | 'sfr' | 'duplex' | 'fourplex' | 'unknown'
    address_raw: str
    address_normalized: str | None
    addr_norm_version: int
    city: str | None
    zip: str | None
    lat: float | None
    lon: float | None
    beds: float | None
    baths: float | None
    sqft: int | None
    asking_rent: int | None
    concessions_text: str | None
    date_posted: dt.date | None
    date_available: dt.date | None
    title: str | None
    body: str | None
    raw_payload_path: str | None


class CollectionStatus(enum.Enum):
    OK = "ok"
    CAPTCHA_BLOCKED = "captcha_blocked"
    ZERO_RESULTS_SUSPICIOUS = "zero_results_suspicious"
    DROP_RATE_ANOMALY = "drop_rate_anomaly"
    PARSER_DRIFT = "parser_drift"
    NETWORK_FAILED = "network_failed"


@dataclass
class CollectionResult:
    """Return value from every source's collect() function."""

    observations: list[RawObservation]
    status: CollectionStatus
    diagnostics: dict[str, Any] = field(default_factory=dict)
