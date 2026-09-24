"""Tests for the comp-source domain blacklist in
``etl.collect_comps_snapshot``.

Wave 6 Task 6.1 (LOW-2 umovefree blacklist) — see CONSOLIDATED_FIX_PLAN.md.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from etl.collect_comps_snapshot import (
    BLACKLIST_DOMAINS,
    _validate_config_urls,
    _validate_url,
)


def test_umovefree_blocked_via_validate_url() -> None:
    """Direct call against `_validate_url` rejects umovefree.com."""
    assert "umovefree.com" in BLACKLIST_DOMAINS

    with pytest.raises(ValueError, match="Blocked domain 'umovefree.com'"):
        _validate_url("https://www.umovefree.com/apartments/dallas-tx")


def test_legitimate_url_passes() -> None:
    """Non-blacklisted hosts (apartments.com, direct property sites) are fine."""
    # Should not raise.
    _validate_url("https://www.apartments.com/the-spoke-austin-tx/c6h96lg/")
    _validate_url("https://www.myashwoodpark.com/floor-plans/")
    _validate_url("https://foxwoodaustin.com/floorplans/")
    _validate_url("")  # empty hostname: no domain to block, no-op


def test_subdomain_of_blocked_also_rejected() -> None:
    """Proper subdomain (`<sub>.umovefree.com`) is rejected."""
    with pytest.raises(ValueError, match="Blocked domain 'umovefree.com'"):
        _validate_url("https://apartments.umovefree.com/listing/123")
    with pytest.raises(ValueError, match="Blocked domain 'umovefree.com'"):
        _validate_url("http://api.umovefree.com/v1/comps")


def test_similar_but_legitimate_domain_passes() -> None:
    """Regression: a domain that merely *contains* the blocked string as a
    substring (e.g. `notumovefree.com`, `myumovefree.com`) is NOT blocked.

    This was the original substring-match false-positive risk surfaced in
    /engineering:code-review on this branch — substring `in` matching would
    have falsely blocked these. The fix uses
    ``host == blocked or host.endswith("." + blocked)`` so exact-match and
    subdomain are caught while similar-named legitimate domains pass.
    """
    # All of these CONTAIN "umovefree.com" as a substring but are NOT
    # the blocked domain or a proper subdomain of it.
    _validate_url("https://notumovefree.com/listing/1")
    _validate_url("https://myumovefree.com/")
    _validate_url("https://umovefree.com.example.org/")  # different TLD


def test_exact_blocked_domain_without_subdomain_rejected() -> None:
    """Exact match on the bare blocked hostname is rejected."""
    with pytest.raises(ValueError, match="Blocked domain 'umovefree.com'"):
        _validate_url("https://umovefree.com/")
    with pytest.raises(ValueError, match="Blocked domain 'umovefree.com'"):
        _validate_url("https://umovefree.com")


def test_blacklist_enforcement_at_config_load(tmp_path: Path) -> None:
    """A YAML config carrying a umovefree URL fails the load-time validator."""
    cfg = {
        "metro": "dallas_tx",
        "comp_monitoring": {
            "subjects": [],
            "comps": [
                {
                    "name": "Bad Comp",
                    "address": "123 Elm St",
                    "direct_floorplans_url": "https://www.umovefree.com/dallas/bad-comp/",
                },
            ],
        },
    }
    cfg_path = tmp_path / "bad_metro.yaml"
    cfg_path.write_text(yaml.safe_dump(cfg), encoding="utf-8")

    parsed = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    with pytest.raises(ValueError, match="Blocked domain 'umovefree.com'"):
        _validate_config_urls(parsed)


def test_clean_config_passes_validation(tmp_path: Path) -> None:
    """Sanity: a config with only legitimate URLs validates cleanly."""
    cfg = {
        "metro": "austin_tx",
        "comp_monitoring": {
            "comps": [
                {
                    "name": "Foxwood",
                    "direct_floorplans_url": "https://foxwoodaustin.com/floorplans/",
                    "apartments_com_url": "https://www.apartments.com/foxwood-austin-tx/3dbg4zf/",
                },
            ],
        },
        "target_comps": ["Sagemont", "Sierra Heights"],  # plain string entries are skipped
    }
    # No raise.
    _validate_config_urls(cfg)


def test_target_comps_dict_with_blocked_url_rejected() -> None:
    """If target_comps entries are dicts with a url, validate them too."""
    cfg = {
        "target_comps": [
            {"name": "Sketchy Comp", "url": "https://www.umovefree.com/foo"},
        ],
    }
    with pytest.raises(ValueError, match="Blocked domain 'umovefree.com'"):
        _validate_config_urls(cfg)
