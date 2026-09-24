"""Tests for etl.gainesville.geocode — Nominatim helper with on-disk cache."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest  # noqa: F401 — used by pytest.approx
from etl.gainesville.geocode import geocode

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_FAKE_RESPONSE = json.dumps([{"lat": "33.6262", "lon": "-97.1330"}]).encode()


def _make_cache(tmp_path: Path, data: dict) -> Path:
    cache = tmp_path / ".geocode_cache.json"
    cache.write_text(json.dumps(data), encoding="utf-8")
    return cache


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_cache_hit_returns_cached_value_without_network_call(tmp_path: Path) -> None:
    """When the address is already in the cache, return cached coords — no HTTP."""
    cache = _make_cache(
        tmp_path,
        {"100 main st, gainesville, tx 76240": [33.626, -97.133]},
    )

    with patch("etl.gainesville.geocode.urllib.request.urlopen") as mock_open:
        result = geocode("100 Main St, Gainesville, TX 76240", cache_path=cache)

    assert result == (33.626, -97.133)
    mock_open.assert_not_called()


def test_cache_hit_negative_entry_returns_none(tmp_path: Path) -> None:
    """A cached ``null`` entry (previous failed lookup) must return None without hitting the API."""
    cache = _make_cache(
        tmp_path,
        {"unknown address, gainesville, tx 76240": None},
    )

    with patch("etl.gainesville.geocode.urllib.request.urlopen") as mock_open:
        result = geocode("Unknown Address, Gainesville, TX 76240", cache_path=cache)

    assert result is None
    mock_open.assert_not_called()


def test_cache_miss_hits_nominatim_saves_and_returns_coords(tmp_path: Path) -> None:
    """Cache miss: call Nominatim, persist result to cache, return coords."""
    cache_path = tmp_path / ".geocode_cache.json"

    mock_resp = MagicMock()
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)
    mock_resp.read.return_value = _FAKE_RESPONSE

    with patch("etl.gainesville.geocode.urllib.request.urlopen", return_value=mock_resp):
        with patch("etl.gainesville.geocode.time.sleep"):  # don't actually wait
            result = geocode("200 Oak Ave, Gainesville, TX 76240", cache_path=cache_path)

    assert result is not None
    lat, lon = result
    assert abs(lat - 33.6262) < 1e-4
    assert abs(lon - (-97.1330)) < 1e-4

    # Verify it was persisted to the cache file.
    assert cache_path.exists()
    saved = json.loads(cache_path.read_text(encoding="utf-8"))
    assert "200 oak ave, gainesville, tx 76240" in saved
    assert saved["200 oak ave, gainesville, tx 76240"] == pytest.approx([33.6262, -97.1330])


def test_empty_address_returns_none(tmp_path: Path) -> None:
    """An empty or whitespace-only address must return None immediately."""
    cache_path = tmp_path / ".geocode_cache.json"

    with patch("etl.gainesville.geocode.urllib.request.urlopen") as mock_open:
        assert geocode("", cache_path=cache_path) is None
        assert geocode("   ", cache_path=cache_path) is None

    mock_open.assert_not_called()
    # Cache file should not be created for empty addresses.
    assert not cache_path.exists()
