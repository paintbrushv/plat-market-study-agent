"""Address geocoding via Nominatim with on-disk cache.

Nominatim usage policy: 1 req/sec, identifying User-Agent, no bulk use.
Cache results to data/local/.geocode_cache.json so repeat runs are free.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

logger = logging.getLogger(__name__)

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
USER_AGENT = (
    "market-study-agent/1.0 (Gainesville TX listings tracker; "
    "maintainers@example.invalid)"
)
RATE_LIMIT_S = 1.1  # slightly above the 1 req/sec public usage limit


def geocode(
    address: str,
    *,
    cache_path: Path,
) -> tuple[float, float] | None:
    """Resolve address to (lat, lon) or None. Caches to cache_path.

    Args:
        address: Full address string (e.g. "100 Main St, Gainesville, TX 76240").
        cache_path: Path to the on-disk JSON cache file. Created automatically
            if it does not exist.

    Returns:
        ``(lat, lon)`` tuple on success, ``None`` when geocoding fails or the
        address is empty.
    """
    if not address or not address.strip():
        return None
    cache = _load_cache(cache_path)
    key = address.strip().lower()
    if key in cache:
        v = cache[key]
        if v is None:
            return None
        return float(v[0]), float(v[1])
    # Cache miss: hit Nominatim.
    coords = _nominatim_lookup(address)
    cache[key] = list(coords) if coords else None
    _save_cache(cache_path, cache)
    return coords


def _nominatim_lookup(address: str) -> tuple[float, float] | None:
    params = urllib.parse.urlencode({
        "q": address,
        "format": "jsonv2",
        "limit": 1,
        "countrycodes": "us",
    })
    url = f"{NOMINATIM_URL}?{params}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    time.sleep(RATE_LIMIT_S)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310
            data = json.loads(resp.read())
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
        logger.warning("Nominatim lookup failed for %r: %s", address, e)
        return None
    if not data:
        return None
    return float(data[0]["lat"]), float(data[0]["lon"])


def _load_cache(cache_path: Path) -> dict[str, list[float] | None]:
    if not cache_path.exists():
        return {}
    try:
        return json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save_cache(cache_path: Path, cache: dict[str, list[float] | None]) -> None:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(cache, indent=2), encoding="utf-8")
