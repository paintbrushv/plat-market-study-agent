from __future__ import annotations

from etl.gainesville.dedup import (
    Tier,
    coord_match_key,
    score_for_tier,
    strict_match_key,
    typo_tolerant_match_key,
    zip_token_match_key,
)


def test_strict_match_key_combines_address_beds_baths() -> None:
    assert strict_match_key("123 main street", 2.0, 1.0) == ("123 main street", 2.0, 1.0)


def test_zip_token_match_key_handles_missing_address() -> None:
    assert zip_token_match_key("76240", "123", "main", 2.0) == ("76240", "123", "main", 2.0)
    assert zip_token_match_key(None, None, None, 2.0) is None


def test_typo_tolerant_match_key_drops_baths() -> None:
    assert typo_tolerant_match_key("123 main street", 2.0) == ("123 main street", 2.0)


def test_coord_match_key_rounds_to_50m_grid() -> None:
    # 50m at lat 33.6 ~ 0.00045 deg; we round to 4 decimals (~11m) for safety.
    k1 = coord_match_key(33.6261, -97.1331, 2.0)
    k2 = coord_match_key(33.6262, -97.1332, 2.0)
    assert k1 == k2  # both round to (33.6261, -97.1331, 2.0)


def test_score_for_tier() -> None:
    assert score_for_tier(Tier.STRICT) == 1.00
    assert score_for_tier(Tier.BATH_FLEX) == 0.92
    assert score_for_tier(Tier.TYPO_TOLERANT) == 0.78
    assert score_for_tier(Tier.COORD) == 0.70
