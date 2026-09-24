from __future__ import annotations

import pytest
from etl.gainesville.owner_normalize import normalize_owner


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("KLEMENT VENTURES LP", "klement ventures"),
        ("KARL KLEMENT LLC", "karl klement"),
        ("KARL KLEMENT L.L.C.", "karl klement"),
        ("ABC HOLDINGS, INC.", "abc holdings"),
        ("XYZ TRUST", "xyz"),
        ("Jane Doe", "jane doe"),
        ("  Smith  Family  Holdings  Co  ", "smith family holdings"),
        ("FOO BAR & ASSOCIATES", "foo bar associates"),
    ],
)
def test_normalize_owner(raw: str, expected: str) -> None:
    assert normalize_owner(raw) == expected


def test_normalize_owner_handles_empty() -> None:
    assert normalize_owner("") == ""
    assert normalize_owner(None) == ""
