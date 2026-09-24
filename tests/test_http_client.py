from __future__ import annotations

import sys
import types

import etl.http_client as http_client


def test_fetch_playwright_uses_stealth_launch_and_webdriver_patch(monkeypatch) -> None:
    launch_calls: list[dict] = []
    context_kwargs: list[dict] = []
    init_scripts: list[str] = []

    class FakeResponse:
        status = 200
        headers = {"content-type": "text/html"}
        url = "https://example.com"

        def json(self):  # noqa: D401
            return {}

    class FakePage:
        def __init__(self) -> None:
            self.handlers = {}

        def on(self, event: str, handler) -> None:  # noqa: ANN001
            self.handlers[event] = handler

        def goto(self, *args, **kwargs):  # noqa: ANN001
            return FakeResponse()

        def wait_for_selector(self, *args, **kwargs) -> None:  # noqa: ANN001
            return None

        def content(self) -> str:
            return "<html><body>ok</body></html>"

    class FakeContext:
        def add_init_script(self, script: str) -> None:
            init_scripts.append(script)

        def new_page(self) -> FakePage:
            return FakePage()

    class FakeBrowser:
        def new_context(self, **kwargs):  # noqa: ANN001
            context_kwargs.append(kwargs)
            return FakeContext()

        def close(self) -> None:
            return None

    class FakeChromium:
        def launch(self, **kwargs):  # noqa: ANN001
            launch_calls.append(kwargs)
            return FakeBrowser()

    class FakePlaywright:
        chromium = FakeChromium()

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb) -> None:  # noqa: ANN001
            return None

    fake_sync_api = types.ModuleType("playwright.sync_api")
    fake_sync_api.sync_playwright = lambda: FakePlaywright()
    monkeypatch.setitem(sys.modules, "playwright.sync_api", fake_sync_api)
    # Pin executable resolution so the assertion is host-independent
    # (resolver behaviour is covered by tests/test_chromium_resolver.py).
    monkeypatch.setattr(
        http_client, "chromium_executable_for", lambda pw: "/fake/chromium"
    )

    status, html, api_data = http_client.fetch_playwright("https://example.com")

    assert status == 200
    assert html == "<html><body>ok</body></html>"
    assert api_data is None
    assert launch_calls == [
        {
            "headless": True,
            "executable_path": "/fake/chromium",
            "args": ["--disable-blink-features=AutomationControlled"],
        }
    ]
    assert context_kwargs == [
        {
            "user_agent": http_client._BROWSER_UA,
            "extra_http_headers": {
                "Accept-Language": "en-US,en;q=0.9",
                "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
            },
        }
    ]
    assert init_scripts == [
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
    ]
