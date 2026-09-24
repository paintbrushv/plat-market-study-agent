"""Tests for the Chromium executable resolver (pmsa-1 follow-up).

The resolver picks which browser binary Playwright should launch:
  1. PMSA_CHROMIUM_PATH env var (must exist),
  2. Playwright's bundled chromium when installed (-> None = default),
  3. snap chromium fallback,
  else a clear RuntimeError naming all three options.

No real browser launches here — filesystem and env are simulated with
tmp_path files and explicit ``env=`` mappings.
"""

from __future__ import annotations

import pytest

from etl.http_client import chromium_executable_for, resolve_chromium_executable


def _touch(tmp_path, name: str) -> str:
    p = tmp_path / name
    p.write_text("")
    return str(p)


# ---------------------------------------------------------------------------
# Precedence 1: PMSA_CHROMIUM_PATH env var
# ---------------------------------------------------------------------------


def test_env_var_wins_over_everything(tmp_path) -> None:
    env_bin = _touch(tmp_path, "env-chromium")
    bundled = _touch(tmp_path, "bundled-chromium")
    snap = _touch(tmp_path, "snap-chromium")

    result = resolve_chromium_executable(
        bundled, env={"PMSA_CHROMIUM_PATH": env_bin}, snap_path=snap
    )
    assert result == env_bin


def test_env_var_set_but_missing_raises(tmp_path) -> None:
    with pytest.raises(RuntimeError, match="PMSA_CHROMIUM_PATH"):
        resolve_chromium_executable(
            None,
            env={"PMSA_CHROMIUM_PATH": str(tmp_path / "nope")},
            snap_path=str(tmp_path / "also-nope"),
        )


def test_blank_env_var_is_ignored(tmp_path) -> None:
    snap = _touch(tmp_path, "snap-chromium")
    result = resolve_chromium_executable(
        None, env={"PMSA_CHROMIUM_PATH": "   "}, snap_path=snap
    )
    assert result == snap


# ---------------------------------------------------------------------------
# Precedence 2: Playwright bundled browser
# ---------------------------------------------------------------------------


def test_bundled_browser_returns_none_for_default_launch(tmp_path) -> None:
    bundled = _touch(tmp_path, "bundled-chromium")
    snap = _touch(tmp_path, "snap-chromium")

    result = resolve_chromium_executable(bundled, env={}, snap_path=snap)
    assert result is None  # None => chromium.launch() uses bundled default


def test_bundled_path_missing_falls_through_to_snap(tmp_path) -> None:
    snap = _touch(tmp_path, "snap-chromium")
    result = resolve_chromium_executable(
        str(tmp_path / "not-installed"), env={}, snap_path=snap
    )
    assert result == snap


def test_bundled_path_none_falls_through_to_snap(tmp_path) -> None:
    snap = _touch(tmp_path, "snap-chromium")
    result = resolve_chromium_executable(None, env={}, snap_path=snap)
    assert result == snap


# ---------------------------------------------------------------------------
# Precedence 3 exhausted: clear error naming all options
# ---------------------------------------------------------------------------


def test_nothing_available_raises_naming_all_three_options(tmp_path) -> None:
    with pytest.raises(RuntimeError) as excinfo:
        resolve_chromium_executable(
            None, env={}, snap_path=str(tmp_path / "no-snap")
        )
    msg = str(excinfo.value)
    assert "PMSA_CHROMIUM_PATH" in msg
    assert "playwright install chromium" in msg
    assert "snap" in msg


# ---------------------------------------------------------------------------
# chromium_executable_for: defensive extraction from a live pw object
# ---------------------------------------------------------------------------


class _FakeChromium:
    def __init__(self, path: str | None, raises: bool = False) -> None:
        self._path = path
        self._raises = raises

    @property
    def executable_path(self) -> str:
        if self._raises:
            raise RuntimeError("browser bundle absent")
        return self._path or ""


class _FakePlaywright:
    def __init__(self, chromium: _FakeChromium) -> None:
        self.chromium = chromium


def test_chromium_executable_for_uses_bundled_when_it_exists(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.delenv("PMSA_CHROMIUM_PATH", raising=False)
    bundled = _touch(tmp_path, "bundled-chromium")
    pw = _FakePlaywright(_FakeChromium(bundled))
    assert chromium_executable_for(pw) is None


def test_chromium_executable_for_survives_raising_executable_path(
    tmp_path, monkeypatch
) -> None:
    # When pw.chromium.executable_path raises, the wrapper must fall through
    # to env/snap resolution rather than crash.
    env_bin = _touch(tmp_path, "env-chromium")
    monkeypatch.setenv("PMSA_CHROMIUM_PATH", env_bin)
    pw = _FakePlaywright(_FakeChromium(None, raises=True))
    assert chromium_executable_for(pw) == env_bin
