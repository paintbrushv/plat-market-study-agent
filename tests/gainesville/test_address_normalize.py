from __future__ import annotations

import pytest
from etl.gainesville.address_normalize import (
    ADDR_NORM_VERSION,
    normalize_address,
    parse_address_parts,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("123 Main St., Gainesville, TX 76240", "123 main street"),
        ("123 Main Street", "123 main street"),
        ("  123  N.  Main  St  ", "123 north main street"),
        ("123 Main St #4", "123 main street"),
        ("123 Main St Apt 4B", "123 main street"),
        ("456 W. Elm Ave., Suite 200", "456 west elm avenue"),
        ("789 SE 4th Pl.", "789 southeast 4th place"),
    ],
)
def test_normalize_address(raw: str, expected: str) -> None:
    assert normalize_address(raw) == expected


def test_normalize_address_returns_none_on_garbage() -> None:
    assert normalize_address("") is None
    assert normalize_address("   ") is None


def test_parse_address_parts_extracts_zip_and_unit() -> None:
    parts = parse_address_parts("123 Main St #4B, Gainesville, TX 76240")
    assert parts.street_number == "123"
    assert parts.street_name_first_token == "main"
    assert parts.unit == "4b"
    assert parts.zip == "76240"


def test_addr_norm_version_is_one() -> None:
    assert ADDR_NORM_VERSION == 1
