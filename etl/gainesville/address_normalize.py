"""Address normalization.

Bumps to ADDR_NORM_VERSION require a one-shot rewrite of historical
raw_observations.address_normalized via renormalize_addresses.py.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

ADDR_NORM_VERSION = 1

_DIRECTIONALS = {
    "n": "north", "s": "south", "e": "east", "w": "west",
    "ne": "northeast", "nw": "northwest", "se": "southeast", "sw": "southwest",
}
_STREET_TYPES = {
    "st": "street", "str": "street", "street": "street",
    "ave": "avenue", "av": "avenue", "avenue": "avenue",
    "rd": "road", "road": "road",
    "blvd": "boulevard", "boulevard": "boulevard",
    "dr": "drive", "drive": "drive",
    "ln": "lane", "lane": "lane",
    "pl": "place", "place": "place",
    "ct": "court", "court": "court",
    "cir": "circle", "circle": "circle",
    "pkwy": "parkway", "parkway": "parkway",
    "hwy": "highway", "highway": "highway",
    "ter": "terrace", "terr": "terrace", "terrace": "terrace",
    "way": "way", "trl": "trail", "trail": "trail",
}
_UNIT_RE = re.compile(
    r"(?:apt|apartment|suite|ste|unit|#)\.?\s*([0-9a-z][0-9a-z\-]*)\b",
    re.IGNORECASE,
)
_ZIP_RE = re.compile(r"\b(\d{5})(?:-\d{4})?\b")


@dataclass(frozen=True)
class AddressParts:
    street_number: str | None
    street_name_first_token: str | None
    unit: str | None
    zip: str | None


def normalize_address(raw: str | None) -> str | None:
    if not raw or not raw.strip():
        return None
    s = unicodedata.normalize("NFC", raw).lower()
    s = _UNIT_RE.sub(" ", s)
    s = re.sub(r"[.,]", " ", s)
    s = re.sub(r"\bsuite\s+\d+", " ", s)
    tokens = s.split()
    out: list[str] = []
    for tok in tokens:
        if tok in _DIRECTIONALS:
            out.append(_DIRECTIONALS[tok])
        elif tok in _STREET_TYPES:
            out.append(_STREET_TYPES[tok])
        elif _ZIP_RE.fullmatch(tok):
            continue  # zip handled separately
        elif re.fullmatch(r"[a-z]{2}", tok):
            # state abbrev or stray
            continue
        elif tok in {"gainesville", "tx", "texas"}:
            continue
        else:
            out.append(tok)
    if not out:
        return None
    return " ".join(out).strip()


def parse_address_parts(raw: str | None) -> AddressParts:
    if not raw:
        return AddressParts(None, None, None, None)
    norm = normalize_address(raw)
    unit_match = _UNIT_RE.search(raw)
    unit = unit_match.group(1).lower() if unit_match else None
    zip_match = _ZIP_RE.search(raw)
    zip_code = zip_match.group(1) if zip_match else None
    street_number = None
    street_name_first_token = None
    if norm:
        toks = norm.split()
        if toks and toks[0].isdigit():
            street_number = toks[0]
            for t in toks[1:]:
                if t not in _DIRECTIONALS.values():
                    street_name_first_token = t
                    break
    return AddressParts(street_number, street_name_first_token, unit, zip_code)
