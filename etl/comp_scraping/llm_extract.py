"""LLM-assisted extraction for property pages no engine recognises.

Loads lazily — the ``anthropic`` import is deferred so the package works
without the SDK installed (the LLM tier is then disabled and the dispatcher
falls back to whatever the engine cascade produced, even if below the quality
gate).

The system prompt is marked ``cache_control: ephemeral`` so a multi-comp
snapshot run pays the full prompt cost once and cache-hit cost for every
subsequent comp inside the 5-minute TTL. At ~12 comps × 4 pages weekly this
keeps total LLM spend under a few cents per snapshot.
"""

from __future__ import annotations

import json
import os
from typing import Any

from etl.comp_scraping._coerce import coerce_float, coerce_int

# Default model. Sonnet 4.6 is the workhorse; bump to Opus 4.7 with
# ``MARKET_STUDY_LLM_QUALITY=1`` for tricky pages.
_DEFAULT_MODEL = "claude-sonnet-4-6"
_QUALITY_MODEL = "claude-opus-4-7"


_SYSTEM_PROMPT = """You are extracting apartment floorplan data from a property website.
Return ONLY valid JSON (no markdown, no commentary) matching this schema:

{
  "floorplans": [
    {
      "name": str,            // floorplan code or descriptor (e.g. "A1", "Studio Loft")
      "beds": int,             // 0 for studio
      "baths": float,          // 1.0, 1.5, 2.0
      "sqft": int,             // square footage; 0 if unknown
      "rent_min": int,         // starting / lowest advertised rent in USD per month; 0 if unknown
      "rent_max": int,         // top of advertised range; equal to rent_min if no range
      "available_units": int   // unit count if listed
    }
  ],
  "specials": [
    {
      "raw_text": str,
      "weeks_free": float | null,   // total weeks of free rent in the offer
      "months_free": float | null,
      "deadline": str | null,       // ISO date if a deadline is given
      "applies_to": str | null
    }
  ],
  "lease_terms": [
    {"months": int, "rent_modifier": int}   // signed delta to rent in USD; 0 if at-rate
  ],
  "confidence": "low" | "medium" | "high",
  "notes": str
}

Rules:
- Only include floorplans where rent_min > 0 OR sqft > 0.
- Cap rents to a sane range: 400 < rent < 15000.
- If no floorplan data is present, return floorplans=[] with confidence="low" and explain in notes.
- Do not invent floorplans or rents that are not on the page.
"""


def is_available() -> bool:
    """Return True when the Anthropic SDK and an API key are present."""

    if not os.environ.get("ANTHROPIC_API_KEY"):
        return False
    try:
        import anthropic  # noqa: F401
    except Exception:
        return False
    return True


def extract_with_llm(
    html_text: str,
    *,
    page_url: str = "",
    quality: bool | None = None,
    max_tokens: int = 2048,
    char_budget: int = 40000,
) -> dict[str, Any]:
    """Call the Anthropic API to extract Shape-B floorplan / specials data.

    Raises ``RuntimeError`` if the SDK isn't available or the API key is missing.
    """

    if not is_available():
        raise RuntimeError(
            "Anthropic SDK or API key unavailable; LLM tier 3 cannot run"
        )

    import anthropic  # local import — gated behind is_available()

    client = anthropic.Anthropic(timeout=90.0)
    use_quality = quality if quality is not None else (
        os.environ.get("MARKET_STUDY_LLM_QUALITY") == "1"
    )
    model = _QUALITY_MODEL if use_quality else _DEFAULT_MODEL

    text = _strip_for_llm(html_text)[:char_budget]
    user_msg = (
        f"Extract floorplan data for: {page_url}\n\n"
        f"Page text (truncated):\n\n{text}"
    )

    response = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=[
            {
                "type": "text",
                "text": _SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=[{"role": "user", "content": user_msg}],
    )

    raw = "".join(
        block.text for block in response.content if getattr(block, "type", None) == "text"
    ).strip()

    payload = _coerce_json(raw)
    return _normalise_llm_output(payload)


def _strip_for_llm(html_text: str) -> str:
    import re

    text = re.sub(r"<script[\s\S]*?</script>", " ", html_text, flags=re.IGNORECASE)
    text = re.sub(r"<style[\s\S]*?</style>", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"<!--[\s\S]*?-->", " ", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _coerce_json(raw: str) -> dict[str, Any]:
    raw = raw.strip()
    if raw.startswith("```"):
        # Strip ```json ... ``` fences that some models emit despite instructions.
        raw = raw.strip("`")
        if raw.lower().startswith("json"):
            raw = raw[4:].lstrip("\n")
        if raw.endswith("```"):
            raw = raw[:-3]
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # Last-ditch: locate the outermost JSON object.
        start = raw.find("{")
        end = raw.rfind("}")
        if start >= 0 and end > start:
            return json.loads(raw[start : end + 1])
        raise


def _normalise_llm_output(payload: dict[str, Any]) -> dict[str, Any]:
    floorplans = []
    for fp in payload.get("floorplans") or []:
        if not isinstance(fp, dict):
            continue
        rent_min = coerce_int(fp.get("rent_min"))
        if rent_min and not (400 < rent_min < 15000):
            continue
        rent_max = coerce_int(fp.get("rent_max")) or rent_min
        floorplans.append(
            {
                "floorplan_name": str(fp.get("name") or ""),
                "beds": coerce_int(fp.get("beds")),
                "baths": coerce_float(fp.get("baths"), default=1.0),
                "sqft": coerce_float(fp.get("sqft")),
                "rent_min": float(rent_min),
                "rent_max": float(rent_max),
                "available_units": coerce_int(fp.get("available_units")),
            }
        )

    specials = []
    for s in payload.get("specials") or []:
        if isinstance(s, dict):
            text = str(s.get("raw_text") or "").strip()
            if text:
                specials.append(text)
        elif isinstance(s, str) and s.strip():
            specials.append(s.strip())

    return {
        "platform": "llm_extracted",
        "floorplans": floorplans,
        "units": [],
        "specials": specials,
        "lease_terms": payload.get("lease_terms") or [],
        "confidence": payload.get("confidence") or "low",
        "notes": payload.get("notes") or "",
    }


