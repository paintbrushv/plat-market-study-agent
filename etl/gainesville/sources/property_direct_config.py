"""Validation for the property_direct YAML config block."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

REQUIRED_SELECTORS = frozenset({
    "floorplan_card", "name", "rent", "beds", "baths", "sqft",
})


class PropertyDirectConfigError(ValueError):
    pass


@dataclass(frozen=True)
class PropertyDirectSite:
    name: str
    url: str
    address: str
    selectors: dict[str, str]
    name_filter: str | None = None


def parse_sites(raw: list[dict[str, Any]]) -> list[PropertyDirectSite]:
    sites: list[PropertyDirectSite] = []
    for i, entry in enumerate(raw):
        if "url" not in entry:
            raise PropertyDirectConfigError(f"site #{i}: missing 'url'")
        if "name" not in entry:
            raise PropertyDirectConfigError(f"site #{i}: missing 'name'")
        if "address" not in entry:
            raise PropertyDirectConfigError(f"site #{i} ({entry.get('name')}): missing 'address'")
        selectors = entry.get("selectors", {})
        missing = REQUIRED_SELECTORS - set(selectors.keys())
        if missing:
            raise PropertyDirectConfigError(
                f"site #{i} ({entry['name']}): missing selector(s): {sorted(missing)}"
            )
        sites.append(PropertyDirectSite(
            name=entry["name"],
            url=entry["url"],
            address=entry["address"],
            selectors=selectors,
            name_filter=entry.get("name_filter") or None,
        ))
    return sites
