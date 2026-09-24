"""Adapter skeleton that would call an LLM-compatible API (e.g., OpenAI)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .generic_http_client import GenericHTTPClient


def _load_meta_prompt() -> str:
    prompt_path = Path("agents/MarketStudyGenerator.md")
    return prompt_path.read_text(encoding="utf-8")


def _load_blocks() -> list[str]:
    blocks_dir = Path("agents/blocks")
    blocks = []
    for block_name in ["web_search_block.md", "inference_notes_block.md", "style_tone_block.md"]:
        block_path = blocks_dir / block_name
        blocks.append(block_path.read_text(encoding="utf-8"))
    return blocks


def _build_messages(config: dict[str, Any], template: str, run_date: str) -> list[dict[str, Any]]:
    system_prompt = _load_meta_prompt() + "\n\n" + "\n\n".join(_load_blocks())
    user_prompt = template
    context_prompt = (
        "Generate an institutional-grade multifamily market study for {metro} with purpose "
        "{purpose}. Use run date {run_date}."
    ).format(metro=config["metro"], purpose=config.get("purpose", "Unknown"), run_date=run_date)

    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": context_prompt},
        {"role": "user", "content": user_prompt},
    ]


def run_agent(config: dict[str, Any], template: str, run_date: str) -> tuple[str, Any]:
    """Execute the agent workflow.

    If the `OPENAI_API_KEY` environment variable is available, this adapter will attempt to call
    the OpenAI Chat Completions endpoint using the meta prompt and section template. Otherwise it
    returns the template and records that no call was made.
    """

    messages = _build_messages(config, template, run_date)

    api_key = os.getenv("OPENAI_API_KEY")
    api_base = os.getenv("OPENAI_API_BASE", "https://api.openai.com/v1")
    model = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")
    temperature = float(os.getenv("OPENAI_TEMPERATURE", "0.2"))

    if api_key:
        client = GenericHTTPClient(
            base_url=api_base,
            default_headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
        )
        try:
            response = client.post(
                "chat/completions",
                payload={
                    "model": model,
                    "messages": messages,
                    "temperature": temperature,
                    "response_format": {"type": "text"},
                },
            )
            content = response["choices"][0]["message"]["content"]
            provenance = {
                "metro": config["metro"],
                "run_date": run_date,
                "model": model,
                "temperature": temperature,
                "messages_preview": messages[:2],
                "http_client_adapter": GenericHTTPClient.__name__,
                "usage": response.get("usage", {}),
            }
            return content, provenance
        except Exception as exc:  # pragma: no cover - network dependent
            provenance = {
                "metro": config["metro"],
                "run_date": run_date,
                "model": model,
                "temperature": temperature,
                "messages_preview": messages[:2],
                "http_client_adapter": GenericHTTPClient.__name__,
                "notes": f"LLM call failed: {exc}",
            }
            return template, provenance

    provenance = {
        "metro": config["metro"],
        "run_date": run_date,
        "messages_preview": messages[:2],
        "notes": "OPENAI_API_KEY not provided; template returned as stub.",
        "http_client_adapter": GenericHTTPClient.__name__,
    }

    return template, provenance
