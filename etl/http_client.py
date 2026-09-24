"""
Multi-method HTTP client for scraping leasing sites.

Fetch cascade (auto mode):
  1. urllib  — free, zero deps, works for open sites
  2. curl_cffi — Chrome TLS fingerprint; bypasses Cloudflare / bot-detection
                 that blocks urllib without requiring a real browser
  3. playwright — headless Chromium; renders JS widgets (RealPage, G5, etc.)

Callers use fetch_html() for simple HTML and fetch_playwright() directly
when they need network-request interception (e.g. to capture RealPage XHR JSON).

Usage
-----
from etl.http_client import fetch_html, fetch_playwright

# auto cascade:
status, html = fetch_html(url)

# force a specific method:
status, html = fetch_html(url, method="curl")
status, html = fetch_html(url, method="playwright")

# playwright with XHR intercept (returns first matching JSON payload):
status, html, api_data = fetch_playwright(
    url,
    intercept_pattern="lrapp.realpage.com",   # substring match on request URL
)
"""

from __future__ import annotations

import datetime as dt
import os
import urllib.error
import urllib.request
from collections.abc import Mapping
from typing import Any

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

_BROWSER_HEADERS = {
    "User-Agent": _BROWSER_UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Cache-Control": "no-cache",
}

# Content is probably a JS shell if it's short and has no meaningful text
_JS_SHELL_MARKERS = ("<div id=\"app\"", "<div id=\"root\"", "Loading...", "__NEXT_DATA__")
_JS_SHELL_MIN_LEN = 3_000  # bytes; below this we suspect a JS shell

# Chromium executable resolution for Playwright launches. Ubuntu 26.04 has no
# Playwright-bundled chromium build (`playwright install chromium` refuses),
# but the snap chromium works with ``executable_path=``.
_CHROMIUM_ENV_VAR = "PMSA_CHROMIUM_PATH"
_SNAP_CHROMIUM_PATH = "/snap/bin/chromium"


def resolve_chromium_executable(
    bundled_path: str | None = None,
    *,
    env: Mapping[str, str] | None = None,
    snap_path: str = _SNAP_CHROMIUM_PATH,
) -> str | None:
    """Resolve which Chromium binary Playwright should launch.

    Precedence:
      1. ``PMSA_CHROMIUM_PATH`` env var, when set (must exist on disk).
      2. Playwright's bundled chromium, when actually installed — signalled
         by ``bundled_path`` existing on disk. Returns ``None`` so
         ``chromium.launch()`` uses its default.
      3. The snap chromium at ``snap_path``, when present.

    Returns the path to pass as ``executable_path=`` (or ``None`` to use
    Playwright's bundled default).

    Raises:
        RuntimeError: when no browser is found, naming all three options.
    """
    env_map: Mapping[str, str] = os.environ if env is None else env
    override = (env_map.get(_CHROMIUM_ENV_VAR) or "").strip()
    if override:
        if not os.path.exists(override):
            raise RuntimeError(
                f"{_CHROMIUM_ENV_VAR}={override!r} is set but does not exist on disk"
            )
        return override

    if bundled_path and os.path.exists(bundled_path):
        return None  # use Playwright's bundled default

    if os.path.exists(snap_path):
        return snap_path

    raise RuntimeError(
        "No Chromium executable found for Playwright. Options: "
        f"(1) set {_CHROMIUM_ENV_VAR} to a Chromium binary path, "
        "(2) install Playwright's bundled browser (`playwright install chromium`), "
        f"(3) install snap chromium at {snap_path} (`snap install chromium`)."
    )


def chromium_executable_for(pw: Any) -> str | None:
    """Resolve ``executable_path`` for a live ``sync_playwright`` instance.

    Thin wrapper over :func:`resolve_chromium_executable` that extracts the
    bundled-browser path from the Playwright object defensively (older
    Playwright versions can raise when the bundle is absent).
    """
    try:
        bundled = pw.chromium.executable_path
    except Exception:
        bundled = None
    return resolve_chromium_executable(bundled)


# ---------------------------------------------------------------------------
# urllib (baseline)
# ---------------------------------------------------------------------------

def fetch_urllib(url: str, timeout_s: int = 30) -> tuple[int, str]:
    """Plain urllib GET with browser-ish headers."""
    import gzip
    import zlib

    req = urllib.request.Request(url, headers=_BROWSER_HEADERS, method="GET")
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        status = int(getattr(resp, "status", 200))
        raw = resp.read()
        encoding = resp.headers.get("Content-Encoding", "").lower()

    if encoding == "gzip":
        raw = gzip.decompress(raw)
    elif encoding == "deflate":
        raw = zlib.decompress(raw)
    elif encoding == "br":
        try:
            import brotli  # type: ignore[import]

            raw = brotli.decompress(raw)
        except ImportError:
            pass  # Fall through — decode will produce garbled text

    return status, raw.decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# curl_cffi (TLS fingerprint spoofing — bypasses Cloudflare / bot detection)
# ---------------------------------------------------------------------------

def fetch_curl(url: str, timeout_s: int = 30) -> tuple[int, str]:
    """
    GET with Chrome TLS fingerprint via curl_cffi.

    Handles gzip/br decompression automatically.
    Raises ImportError if curl_cffi is not installed.
    """
    try:
        from curl_cffi import requests as _curl  # type: ignore[import]
    except ImportError as exc:
        raise ImportError(
            "curl_cffi is not installed. Run: uv add curl-cffi"
        ) from exc

    resp = _curl.get(
        url,
        impersonate="chrome124",
        timeout=timeout_s,
        headers={"Accept-Language": "en-US,en;q=0.9"},
        allow_redirects=True,
    )
    return resp.status_code, resp.text


# ---------------------------------------------------------------------------
# Playwright (headless Chromium — renders JS widgets)
# ---------------------------------------------------------------------------

def fetch_playwright(
    url: str,
    timeout_s: int = 45,
    wait_selector: str | None = None,
    intercept_pattern: str | None = None,
) -> tuple[int, str, Any]:
    """
    Fetch a page with headless Chromium via Playwright.

    Parameters
    ----------
    url             : Page URL to load.
    timeout_s       : Navigation timeout in seconds.
    wait_selector   : CSS selector to wait for before returning HTML
                      (e.g. ".fp-floorplan-list"). Falls back to networkidle.
    intercept_pattern : Substring to match against outgoing request URLs.
                        First matching JSON response body is returned as the
                        third element of the tuple (parsed dict/list).
                        Pass None to skip interception.

    Returns
    -------
    (http_status, page_html, intercepted_json)
    http_status     : Final HTTP status code (200 if navigation succeeded).
    page_html       : Full rendered HTML after JS execution.
    intercepted_json: Parsed JSON from the first XHR/fetch matching
                      intercept_pattern, or None.

    Raises ImportError if playwright is not installed / browsers not downloaded.
    """
    try:
        from playwright.sync_api import sync_playwright  # type: ignore[import]
    except ImportError as exc:
        raise ImportError(
            "playwright is not installed. Run: uv add playwright && playwright install chromium"
        ) from exc

    captured_json: Any = None
    http_status = 200

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            executable_path=chromium_executable_for(pw),
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = browser.new_context(
            user_agent=_BROWSER_UA,
            extra_http_headers={
                "Accept-Language": "en-US,en;q=0.9",
                "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
            },
        )
        context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )
        page = context.new_page()

        # Intercept XHR/fetch responses matching pattern
        if intercept_pattern:
            def _on_response(response: Any) -> None:
                nonlocal captured_json
                if captured_json is not None:
                    return
                if intercept_pattern in response.url:
                    ct = response.headers.get("content-type", "")
                    if "json" in ct:
                        try:
                            captured_json = response.json()
                        except Exception:
                            pass

            page.on("response", _on_response)

        try:
            resp = page.goto(
                url,
                wait_until="networkidle",
                timeout=timeout_s * 1_000,
            )
            if resp:
                http_status = resp.status
            if wait_selector:
                page.wait_for_selector(wait_selector, timeout=10_000)
        except Exception:
            # Timeout or navigation error — return whatever rendered so far
            pass

        html = page.content()
        browser.close()

    return http_status, html, captured_json


# ---------------------------------------------------------------------------
# Auto cascade
# ---------------------------------------------------------------------------

def _looks_like_js_shell(html: str) -> bool:
    """True if the HTML looks like an empty JS app shell with no real content."""
    if len(html) < _JS_SHELL_MIN_LEN:
        return True
    return any(marker in html for marker in _JS_SHELL_MARKERS)


def fetch_html(
    url: str,
    method: str = "auto",
    timeout_s: int = 30,
) -> tuple[int, str]:
    """
    Fetch HTML with the specified method (or auto-cascade).

    method
    ------
    "auto"       Try urllib → curl → playwright, upgrading on 403/shell.
    "urllib"     Plain urllib only.
    "curl"       curl_cffi only (Chrome TLS fingerprint).
    "playwright" Headless Chromium only (returns rendered HTML).

    Returns (status_code, html_text).
    """
    if method == "urllib":
        return fetch_urllib(url, timeout_s)

    if method == "curl":
        return fetch_curl(url, timeout_s)

    if method == "playwright":
        status, html, _ = fetch_playwright(url, timeout_s)
        return status, html

    # ── auto cascade ──────────────────────────────────────────────────────
    # Step 1: urllib
    try:
        status, html = fetch_urllib(url, timeout_s)
        if status < 400 and not _looks_like_js_shell(html):
            return status, html
        blocked_by_urllib = status in (403, 429)
    except urllib.error.HTTPError as exc:
        blocked_by_urllib = exc.code in (403, 429)
        status, html = exc.code, ""
    except Exception:
        blocked_by_urllib = False
        status, html = 0, ""

    # Step 2: curl_cffi
    try:
        status, html = fetch_curl(url, timeout_s)
        if status < 400 and not _looks_like_js_shell(html):
            return status, html
        blocked_by_curl = status in (403, 429)
    except ImportError:
        # curl_cffi not installed — skip straight to playwright
        blocked_by_curl = blocked_by_urllib
    except Exception:
        blocked_by_curl = False

    # Step 3: playwright (only if we have a signal something is blocking/rendering)
    if blocked_by_urllib or blocked_by_curl or _looks_like_js_shell(html):
        try:
            status, html, _ = fetch_playwright(url, timeout_s=max(timeout_s, 45))
            return status, html
        except ImportError:
            pass  # playwright not installed — return best result so far
        except Exception:
            pass

    return status, html


# ---------------------------------------------------------------------------
# safe_fetch wrapper (matches signature used across ETL scripts)
# ---------------------------------------------------------------------------

def safe_fetch(
    url: str,
    timeout_s: int = 30,
    method: str = "auto",
) -> dict[str, Any]:
    """
    Fetch url and return a result dict compatible with the legacy safe_fetch_text
    signature used in collect_comps_snapshot.py and collect_new_builds_snapshot.py.

    Keys: ok, url, status, text, as_of_utc, error
    """
    started = dt.datetime.now(dt.UTC)
    try:
        status, text = fetch_html(url, method=method, timeout_s=timeout_s)
        return {
            "ok": status < 400,
            "url": url,
            "status": status,
            "text": text,
            "as_of_utc": started.isoformat(),
            "error": None,
        }
    except urllib.error.HTTPError as exc:
        return {
            "ok": False,
            "url": url,
            "status": exc.code,
            "text": "",
            "as_of_utc": started.isoformat(),
            "error": str(exc),
        }
    except Exception as exc:
        return {
            "ok": False,
            "url": url,
            "status": None,
            "text": "",
            "as_of_utc": started.isoformat(),
            "error": str(exc),
        }
