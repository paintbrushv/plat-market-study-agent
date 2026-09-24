"""Lightweight HTTP client adapter used by agent runners."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, cast


@dataclass
class GenericHTTPClient:
    """Minimal HTTP client wrapper.

    Replace or extend this adapter with a vendor SDK if desired. The default
    implementation keeps dependencies minimal by relying on `urllib`.
    """

    base_url: str
    default_headers: dict[str, str] = field(default_factory=dict)
    timeout: int = 60

    def _build_url(self, path: str) -> str:
        if path.startswith("http://") or path.startswith("https://"):
            return path
        return f"{self.base_url.rstrip('/')}/{path.lstrip('/')}"

    def post(
        self,
        path: str,
        payload: dict[str, Any],
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """POST JSON to the configured endpoint.

        Raise `RuntimeError` with body details on non-2xx responses for easier debugging.
        """

        request_headers = {"Content-Type": "application/json", **self.default_headers}
        if headers:
            request_headers.update(headers)

        url = self._build_url(path)
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers=request_headers, method="POST")

        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as response:
                body = response.read().decode("utf-8")
                payload_json = cast(dict[str, Any], json.loads(body)) if body else {}
                return payload_json
        except urllib.error.HTTPError as exc:
            error_body = exc.read().decode("utf-8") if exc.fp else exc.reason
            raise RuntimeError(f"HTTP {exc.code} calling {url}: {error_body}") from exc
        except urllib.error.URLError as exc:  # pragma: no cover - network failures
            raise RuntimeError(f"Failed to call {url}: {exc.reason}") from exc
