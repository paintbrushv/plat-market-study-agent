"""Field-level validation. Out-of-range values are nulled with a
diagnostic; the original is preserved on disk via raw_payload_path.
"""

from __future__ import annotations

import re
from dataclasses import replace

from etl.gainesville.dataclasses import RawObservation

RENT_MIN, RENT_MAX = 200, 10_000
BEDS_MIN, BEDS_MAX = 0.0, 8.0
BATHS_MIN, BATHS_MAX = 0.0, 8.0
SQFT_MIN, SQFT_MAX = 100, 8_000


def validate_and_clean(obs: RawObservation) -> tuple[RawObservation, list[str]]:
    issues: list[str] = []
    updates: dict[str, object] = {}
    if obs.asking_rent is not None and not (RENT_MIN <= obs.asking_rent <= RENT_MAX):
        issues.append("asking_rent_out_of_range")
        updates["asking_rent"] = None
    if obs.beds is not None and not (BEDS_MIN <= obs.beds <= BEDS_MAX):
        issues.append("beds_out_of_range")
        updates["beds"] = None
    if obs.baths is not None and not (BATHS_MIN <= obs.baths <= BATHS_MAX):
        issues.append("baths_out_of_range")
        updates["baths"] = None
    if obs.sqft is not None and not (SQFT_MIN <= obs.sqft <= SQFT_MAX):
        issues.append("sqft_out_of_range")
        updates["sqft"] = None
    cleaned = replace(obs, **updates) if updates else obs
    return cleaned, issues


# ---------------------------------------------------------------------------
# For-sale-disguised-as-rental detector
# ---------------------------------------------------------------------------

# Strong signals: any single match is sufficient to classify as for-sale.
_STRONG_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("turbotenant_link", re.compile(r"turbotenant", re.IGNORECASE)),
    ("mortgage_payment_estimate", re.compile(
        r"estimated\s+(?:monthly\s+)?mortgage|monthly\s+mortgage\s+payment|mortgage\s+payment\s+estimate",
        re.IGNORECASE,
    )),
    ("price_reduced", re.compile(r"price\s+reduced|price\s+drop|reduced\s+price", re.IGNORECASE)),
    ("fsbo", re.compile(r"\bfsbo\b|for\s+sale\s+by\s+owner", re.IGNORECASE)),
    ("list_price", re.compile(r"\blist(?:ing)?\s+price\b", re.IGNORECASE)),
    ("asking_price", re.compile(r"\basking\s+price\b", re.IGNORECASE)),
    ("seller_financing", re.compile(
        r"\bseller\s+financing\b|\bowner\s+financing\b", re.IGNORECASE
    )),
    ("home_for_sale", re.compile(r"\bhome\s+for\s+sale\b", re.IGNORECASE)),
]

# Weak signals: require 2 or more to classify as for-sale.
_WEAK_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("appraised_at", re.compile(r"\bappraised\s+at\b", re.IGNORECASE)),
    ("down_payment", re.compile(r"\bdown\s+payment\b", re.IGNORECASE)),
    ("closing_costs", re.compile(r"\bclosing\s+costs?\b", re.IGNORECASE)),
    ("property_tax_with_hoa_or_insurance", re.compile(
        r"(?=.*\bproperty\s+tax\b)(?=.*(?:\bhoa\b|\binsurance\b))",
        re.IGNORECASE | re.DOTALL,
    )),
]


def is_for_sale_disguised(obs: RawObservation) -> str | None:
    """If the observation looks like a for-sale listing posted under a
    rental category, return a short reason string. Otherwise None.

    Heuristics fire on body + title text. None of them are individually
    decisive — they're tuned to catch the common patterns seen on
    Craigslist (Turbotenant prescreener links, mortgage payment estimates,
    'list price' phrasing). Conservative on purpose: a false negative
    just keeps a sale listing in the tracker, but a false positive drops
    a legitimate rental.
    """
    text = (obs.title or "") + " " + (obs.body or "")

    # Check strong signals first.
    for label, pattern in _STRONG_PATTERNS:
        if pattern.search(text):
            return label

    # Check weak signals — need 2 or more to fire.
    matched_weak: list[str] = []
    for label, pattern in _WEAK_PATTERNS:
        if pattern.search(text):
            matched_weak.append(label)
    if len(matched_weak) >= 2:
        return "two_weak_signals: " + ", ".join(matched_weak[:2])

    return None
