"""Generic owner-name normalization.

Out of scope: serious entity matching across name variants. This is a
small collapse-the-obvious-noise helper. Adopt a real matcher only if
the MF tracker's owner-concentration table starts showing meaningful
fragmentation across LLC/INC suffix variants.
"""

from __future__ import annotations

import re
import unicodedata

# Tokens we always drop, anywhere in the string.
# These are pure legal-entity suffixes that carry no name signal.
# NOTE: "associates", "ventures", "holdings" are NOT dropped — they are
# often substantive parts of the owner name (e.g., "Klement Ventures").
_DROP_ALWAYS = frozenset({
    "llc", "lc", "ltd", "lp", "llp", "inc", "incorporated",
    "corp", "corporation", "co", "company", "trust", "tr",
    "partners", "partnership",
    "&",
})


def normalize_owner(raw: str | None) -> str:
    if not raw:
        return ""
    s = unicodedata.normalize("NFC", raw).lower()
    # Collapse "L.L.C." -> "llc" and "L.P." -> "lp" BEFORE we strip dots.
    s = re.sub(r"\b([a-z])\.([a-z])\.([a-z])\.", r"\1\2\3", s)
    s = re.sub(r"\b([a-z])\.([a-z])\.", r"\1\2", s)
    s = re.sub(r"[.,]", " ", s)
    tokens = [t for t in s.split() if t not in _DROP_ALWAYS]
    return " ".join(tokens).strip()
