from __future__ import annotations

"""Tests that infra failures (browser/network errors) set needs_parser_or_manual_review.

These import the REAL production helper (previously the logic was inlined in
main() and the tests asserted against a copy — review finding)."""

from etl.collect_comps_snapshot import _flag_infra_failure
from etl.http_client import resolve_chromium_executable

import pytest


def test_playwright_error_sets_flag() -> None:
    meta = {"passes_gate": False, "errors": ["playwright: browser not found"]}
    comp_record: dict = {}
    _flag_infra_failure(meta, comp_record)
    assert comp_record.get("needs_parser_or_manual_review") is True
    assert meta.get("needs_parser_or_manual_review") is True


def test_chromium_error_sets_flag() -> None:
    meta = {"passes_gate": False, "errors": ["Failed to launch Chromium"]}
    comp_record: dict = {}
    _flag_infra_failure(meta, comp_record)
    assert comp_record.get("needs_parser_or_manual_review") is True


def test_browser_not_installed_sets_flag() -> None:
    meta = {"passes_gate": False, "errors": ["Browser executable not found"]}
    comp_record: dict = {}
    _flag_infra_failure(meta, comp_record)
    assert comp_record.get("needs_parser_or_manual_review") is True


def test_timeout_error_sets_flag() -> None:
    meta = {"passes_gate": False, "errors": ["Timeout waiting for selector"]}
    comp_record: dict = {}
    _flag_infra_failure(meta, comp_record)
    assert comp_record.get("needs_parser_or_manual_review") is True


def test_connection_refused_sets_flag() -> None:
    meta = {"passes_gate": False, "errors": ["Connection refused: 127.0.0.1:9222"]}
    comp_record: dict = {}
    _flag_infra_failure(meta, comp_record)
    assert comp_record.get("needs_parser_or_manual_review") is True


def test_chromium_resolver_error_string_sets_flag(tmp_path) -> None:
    """The resolver's own RuntimeError text must trigger the flag (review
    finding: 'executablepath' keyword missed 'No Chromium executable found')."""
    with pytest.raises(RuntimeError) as excinfo:
        resolve_chromium_executable(None, env={}, snap_path=str(tmp_path / "no-snap"))
    meta = {"passes_gate": False, "errors": [str(excinfo.value)]}
    comp_record: dict = {}
    _flag_infra_failure(meta, comp_record)
    assert comp_record.get("needs_parser_or_manual_review") is True


@pytest.mark.parametrize(
    "error",
    [
        "DNS lookup failed for example.com",
        "Temporary failure in name resolution",
        "TLS handshake failed",
        "SSL: CERTIFICATE_VERIFY_FAILED",
        "proxy connection failed",
        "HTTP 502 Bad Gateway",
        "HTTP 503 Service Unavailable",
        "HTTP 504 Gateway Timeout",
        "Connection reset by peer",
    ],
)
def test_network_infra_errors_set_flag(error: str) -> None:
    meta = {"passes_gate": False, "errors": [error]}
    comp_record: dict = {}
    _flag_infra_failure(meta, comp_record)
    assert comp_record.get("needs_parser_or_manual_review") is True


def test_non_infra_error_does_not_set_flag() -> None:
    """A gate failure from a parsing/schema mismatch should NOT trigger the flag."""
    meta = {"passes_gate": False, "errors": ["No floorplans found in HTML"]}
    comp_record: dict = {}
    _flag_infra_failure(meta, comp_record)
    assert comp_record.get("needs_parser_or_manual_review") is None


def test_passes_gate_true_does_not_set_flag() -> None:
    """Even if the error list contains a keyword, passing the gate should skip the flag."""
    meta = {"passes_gate": True, "errors": ["playwright: browser not found"]}
    comp_record: dict = {}
    _flag_infra_failure(meta, comp_record)
    assert comp_record.get("needs_parser_or_manual_review") is None


def test_empty_errors_does_not_set_flag() -> None:
    meta = {"passes_gate": False, "errors": []}
    comp_record: dict = {}
    _flag_infra_failure(meta, comp_record)
    assert comp_record.get("needs_parser_or_manual_review") is None


def test_no_such_file_sets_flag() -> None:
    meta = {"passes_gate": False, "errors": ["No such file or directory: /usr/bin/chromium"]}
    comp_record: dict = {}
    _flag_infra_failure(meta, comp_record)
    assert comp_record.get("needs_parser_or_manual_review") is True
