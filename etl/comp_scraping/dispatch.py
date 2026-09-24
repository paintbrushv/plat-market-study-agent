"""Engine resolver and per-comp orchestrator.

Detection priority:
1. URL substring match against ``EngineModule.url_substrings``.
2. HTML fingerprint match against an already-fetched base page.
3. LLM fallback (synthetic engine, Tier 3).

The orchestrator (``collect_comp``) drives the per-comp workflow:

    base fetch  →  detect engine  →  fetch declared pages  →  parse each
    →  merge partials  →  quality_gate  →  (optional) LLM fallback
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

from etl.comp_scraping import quality_gate
from etl.comp_scraping.normalize import normalize_shape
from etl.comp_scraping.types import EngineModule, FetchFn, PageSpec, ScrapeResult


def resolve_engine(
    url: str,
    base_html: str,
    registry: Iterable[EngineModule],
    *,
    fallback: EngineModule | None = None,
) -> EngineModule:
    """Pick the best engine for a URL / sample of HTML.

    Returns ``fallback`` (the LLM engine) when no engine claims the comp.
    Caller is expected to register the LLM engine separately and pass it as
    ``fallback`` rather than placing it in ``registry``.
    """

    url_l = (url or "").lower()
    for module in registry:
        for sub in module.url_substrings:
            if sub and sub.lower() in url_l:
                return module

    if base_html:
        for module in registry:
            for fp in module.html_fingerprints:
                if fp and fp in base_html:
                    return module

    if fallback is None:
        raise ValueError("No engine matched and no fallback provided")
    return fallback


def _interpolate(template: str, base_url: str) -> str:
    base_clean = base_url.rstrip("/")
    if "{base}" in template:
        return template.format(base=base_clean)
    if template.startswith("http://") or template.startswith("https://"):
        return template
    if template.startswith("/"):
        return base_clean + template
    return f"{base_clean}/{template}"


def fetch_pages(
    base_url: str,
    specs: list[PageSpec],
    fetch_fn: FetchFn,
    *,
    base_html: str | None = None,
    prefetched: dict[tuple[str, str], dict[str, Any]] | None = None,
    timeout_s: int = 30,
) -> list[tuple[PageSpec, dict[str, Any]]]:
    """Fetch each declared page, reusing matching prefetched responses or HTML.

    Returns a list of ``(spec, fetch_result)`` pairs preserving order. Each
    ``fetch_result`` is a dict with at least ``ok``, ``status``, ``text``,
    ``error``, ``as_of_utc`` (matching ``etl.http_client.safe_fetch``).
    """

    out: list[tuple[PageSpec, dict[str, Any]]] = []
    for spec in specs:
        url = _interpolate(spec.url_template, base_url)
        cached = (
            prefetched.get((url.rstrip("/"), spec.fetch_method))
            if prefetched
            else None
        )
        if cached is not None:
            out.append((spec, cached))
            continue
        if (
            base_html
            and spec.fetch_method != "playwright"
            and url.rstrip("/") == base_url.rstrip("/")
        ):
            out.append(
                (spec, {"ok": True, "status": 200, "text": base_html, "error": None})
            )
            continue
        try:
            res = fetch_fn(url, spec.fetch_method, timeout_s)
        except Exception as exc:  # pragma: no cover - network dependent
            res = {"ok": False, "status": None, "text": "", "error": str(exc)}
        out.append((spec, res))
    return out


def run_engine(
    *,
    module: EngineModule,
    base_url: str,
    base_html: str,
    fetch_fn: FetchFn,
    prefetched: dict[tuple[str, str], dict[str, Any]] | None = None,
    timeout_s: int = 30,
) -> ScrapeResult:
    """Execute one engine end-to-end against a comp's base URL."""

    specs = module.discover_pages(base_url)
    fetched = fetch_pages(
        base_url,
        specs,
        fetch_fn,
        base_html=base_html,
        prefetched=prefetched,
        timeout_s=timeout_s,
    )

    partials = []
    pages_fetched: list[str] = []
    errors: list[str] = []
    for spec, res in fetched:
        if not res.get("ok"):
            if spec.required:
                errors.append(
                    f"{module.name}: required page '{spec.name}' fetch failed: "
                    f"{res.get('error') or res.get('status')}"
                )
            continue
        pages_fetched.append(spec.name)
        try:
            partial = module.parse_page(spec, res.get("text") or "")
            partials.append(partial)
        except ValueError:
            # Engines raise ValueError when fingerprint absent. Soft-fail.
            continue
        except Exception as exc:  # pragma: no cover - parser dependent
            errors.append(f"{module.name}: parse '{spec.name}' raised {exc}")

    result = module.merge(partials)
    result.pages_fetched = pages_fetched
    result.errors.extend(errors)
    return result


def collect_comp(
    *,
    base_url: str,
    units_total: int | None,
    fetch_fn: FetchFn,
    registry: list[EngineModule],
    fallback: EngineModule | None = None,
    timeout_s: int = 30,
    extraction_queue_dir: str | None = None,
    comp_name: str | None = None,
) -> dict[str, Any]:
    """Top-level per-comp scrape. Returns a Shape-B dict ready for the
    snapshot's ``direct`` field, plus ``scrape_meta`` for diagnostics.

    ``fallback`` is the LLM engine. When ``None``, comps that no engine
    recognises return an empty Shape-B with an error appended.
    """

    base_fetch = fetch_fn(base_url, "auto", timeout_s)
    base_html = base_fetch.get("text") or "" if base_fetch.get("ok") else ""
    prefetched: dict[tuple[str, str], dict[str, Any]] = {}
    if base_fetch.get("ok"):
        prefetched[(base_url.rstrip("/"), "auto")] = base_fetch

    floorplans_html = ""
    alternate_floorplans_html = ""
    resolution_error: ValueError | None = None
    try:
        module = resolve_engine(base_url, base_html, registry, fallback=fallback)
    except ValueError as exc:
        module = None
        resolution_error = exc

    # Many engines fingerprint cleanly only on the floorplan page (e.g.
    # SightMap iframes are absent from the property homepage). Probe the
    # canonical route first, but only when the URL and homepage were
    # insufficient to resolve an engine.
    if module is None or module is fallback:
        floorplans_url = f"{base_url.rstrip('/')}/floorplans/"
        floorplans_fetch = fetch_fn(floorplans_url, "auto", timeout_s)
        if floorplans_fetch.get("ok"):
            prefetched[(floorplans_url.rstrip("/"), "auto")] = floorplans_fetch
            floorplans_html = floorplans_fetch.get("text") or ""
        try:
            module = resolve_engine(
                base_url,
                base_html + "\n\n" + floorplans_html,
                registry,
                fallback=fallback,
            )
        except ValueError as exc:
            module = None
            resolution_error = exc

    # Some property sites use ``/floor-plans/`` instead. Only incur the
    # alternate probe if the canonical route still did not identify an engine.
    if module is None or module is fallback:
        alternate_floorplans_url = f"{base_url.rstrip('/')}/floor-plans/"
        alternate_floorplans_fetch = fetch_fn(
            alternate_floorplans_url, "auto", timeout_s
        )
        if alternate_floorplans_fetch.get("ok"):
            prefetched[
                (alternate_floorplans_url.rstrip("/"), "auto")
            ] = alternate_floorplans_fetch
            alternate_floorplans_html = (
                alternate_floorplans_fetch.get("text") or ""
            )
        try:
            module = resolve_engine(
                base_url,
                base_html
                + "\n\n"
                + floorplans_html
                + "\n\n"
                + alternate_floorplans_html,
                registry,
                fallback=fallback,
            )
        except ValueError as exc:
            module = None
            resolution_error = exc

    if module is None:
        exc = resolution_error or ValueError(
            "No engine matched and no fallback provided"
        )
        return {
            "platform": "unknown",
            "floorplans": [],
            "units": [],
            "specials": [],
            "scrape_meta": {
                "engine": "none",
                "confidence": "low",
                "errors": [str(exc)],
                "pages_fetched": [],
            },
        }

    result = run_engine(
        module=module,
        base_url=base_url,
        base_html=base_html,
        fetch_fn=fetch_fn,
        prefetched=prefetched,
        timeout_s=timeout_s,
    )

    payload = normalize_shape(
        {
            "platform": result.platform,
            "floorplans": result.floorplans,
            "units": result.units,
            "specials": result.specials,
        }
    )

    threshold = quality_gate.min_threshold(units_total)
    gate_pass = quality_gate.passes(payload, min_plans=threshold)

    # LLM Tier-3 escalation: only when first engine wasn't already the LLM.
    if not gate_pass and fallback is not None and module is not fallback:
        llm_result = run_engine(
            module=fallback,
            base_url=base_url,
            base_html=base_html,
            fetch_fn=fetch_fn,
            timeout_s=timeout_s,
            prefetched=prefetched,
        )
        llm_payload = normalize_shape(
            {
                "platform": llm_result.platform,
                "floorplans": llm_result.floorplans,
                "units": llm_result.units,
                "specials": llm_result.specials,
            }
        )
        if quality_gate.passes(llm_payload, min_plans=threshold):
            payload = llm_payload
            result = llm_result
            gate_pass = True

    payload["scrape_meta"] = {
        "engine": result.platform,
        "confidence": result.confidence if gate_pass else "low",
        "pages_fetched": result.pages_fetched,
        "errors": result.errors,
        "min_threshold": threshold,
        "passes_gate": gate_pass,
    }

    # When the gate fails and an extraction queue dir is set, dump the
    # richest HTML we have so a Claude Code subagent (Option A) can extract
    # Shape-B floorplans as a follow-up pass. Prefer the floorplans-page
    # HTML; fall back to the base homepage; final fallback is a fresh
    # Playwright fetch (catches sites whose static fetch was blocked but
    # which a real browser can reach).
    if not gate_pass and extraction_queue_dir and comp_name:
        queue_html = floorplans_html or alternate_floorplans_html or base_html
        if not queue_html:
            try:
                pw = fetch_fn(base_url, "playwright", timeout_s)
                if pw.get("ok"):
                    queue_html = pw.get("text") or ""
            except Exception:
                queue_html = ""

        if queue_html:
            payload["scrape_meta"]["needs_llm"] = True
            try:
                from pathlib import Path

                queue_dir = Path(extraction_queue_dir)
                queue_dir.mkdir(parents=True, exist_ok=True)
                slug = re.sub(r"[^a-z0-9]+", "_", comp_name.lower()).strip("_")
                html_path = queue_dir / f"{slug}.html"
                html_path.write_text(queue_html, encoding="utf-8")
                payload["scrape_meta"]["llm_queue_html"] = str(html_path)
            except Exception as exc:
                payload["scrape_meta"]["errors"].append(
                    f"queue write failed: {exc}"
                )
    return payload
