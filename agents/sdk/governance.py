import json
from typing import Any


async def deny_uncited_section(input_data: dict[str, Any], tool_use_id: str, context: Any) -> dict[str, Any]:
    tool_name = input_data.get("tool_name", "")
    if not tool_name.endswith("__generate_section"):
        return {}
    evidence = input_data.get("tool_input", {}).get("evidence", [])
    if not _is_valid_evidence(evidence):
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": (
                    "Data governance: section requires non-empty evidence. "
                    "Each entry must be either (a) a dict with a 'source' key, "
                    "or (b) a JSON-encoded payload whose envelope has a 'sources' "
                    "key, or whose list rows each have a 'source' key."
                ),
            }
        }
    return {}


def _is_valid_evidence(evidence: Any) -> bool:
    """Accept dict-with-source entries OR JSON-encoded payload strings.

    Two valid string shapes (decoded):
      1. ``{"rows": [...], "sources": [...]}``-shaped envelope where
         ``sources`` is non-empty (matches ``pull_comps`` output).
      2. A list of dicts where every dict has a ``source`` key.
    """
    if not isinstance(evidence, list) or not evidence:
        return False
    for entry in evidence:
        if isinstance(entry, dict):
            if not entry.get("source"):
                return False
            continue
        if isinstance(entry, str):
            try:
                payload = json.loads(entry)
            except (json.JSONDecodeError, TypeError, ValueError):
                return False
            if isinstance(payload, dict):
                sources = payload.get("sources")
                if isinstance(sources, list) and sources:
                    continue
                return False
            if isinstance(payload, list):
                if all(isinstance(r, dict) and r.get("source") for r in payload):
                    continue
                return False
            return False
        return False
    return True
