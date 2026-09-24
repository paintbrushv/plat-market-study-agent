import asyncio
import json

from agents.sdk.governance import deny_uncited_section


def _run(c): return asyncio.run(c)


def test_allows_section_with_well_formed_evidence():
    result = _run(deny_uncited_section(
        {"tool_name": "mcp__ms__generate_section",
         "tool_input": {"section": "X", "evidence": [{"source": "CoStar", "row_id": 1}]}},
        "id", None,
    ))
    assert result == {}


def test_denies_section_without_evidence():
    result = _run(deny_uncited_section(
        {"tool_name": "mcp__ms__generate_section",
         "tool_input": {"section": "X", "evidence": []}},
        "id", None,
    ))
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_denies_section_with_evidence_missing_source():
    result = _run(deny_uncited_section(
        {"tool_name": "mcp__ms__generate_section",
         "tool_input": {"section": "X", "evidence": [{"row_id": 1}]}},
        "id", None,
    ))
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_denies_section_with_non_dict_evidence_entries():
    result = _run(deny_uncited_section(
        {"tool_name": "mcp__ms__generate_section",
         "tool_input": {"section": "X", "evidence": [None, ""]}},
        "id", None,
    ))
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_hook_matches_by_suffix_so_server_rename_is_safe():
    """If someone renames the MCP server from 'ms' to 'market_study', the hook still fires."""
    result = _run(deny_uncited_section(
        {"tool_name": "mcp__market_study__generate_section",
         "tool_input": {"section": "X", "evidence": []}},
        "id", None,
    ))
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_allows_unrelated_tools():
    result = _run(deny_uncited_section(
        {"tool_name": "mcp__ms__pull_comps", "tool_input": {}},
        "id", None,
    ))
    assert result == {}


def test_governance_accepts_json_encoded_rows_payload():
    """The agent now passes the JSON payload string from pull_comps as a
    single evidence entry. If the decoded payload has a 'sources' key
    (envelope shape), the hook must allow it."""
    payload = json.dumps({
        "row_count": 2,
        "rows": [{"name": "A", "source": "snapshot:foo"}, {"name": "B", "source": "snapshot:foo"}],
        "sources": ["snapshot:foo"],
    })
    result = _run(deny_uncited_section(
        {"tool_name": "mcp__ms__generate_section",
         "tool_input": {"section": "Comp Set Overview", "evidence": [payload]}},
        "id", None,
    ))
    assert result == {}


def test_governance_accepts_json_encoded_list_of_sourced_rows():
    """A JSON list of dicts where every dict has a 'source' is also allowed."""
    payload = json.dumps([
        {"name": "A", "source": "snapshot:foo"},
        {"name": "B", "source": "snapshot:foo"},
    ])
    result = _run(deny_uncited_section(
        {"tool_name": "mcp__ms__generate_section",
         "tool_input": {"section": "Comp Set Overview", "evidence": [payload]}},
        "id", None,
    ))
    assert result == {}


def test_governance_rejects_json_encoded_payload_without_sources():
    """A JSON envelope dict without a 'sources' key (or with empty sources)
    has no provable attribution — deny."""
    payload = json.dumps({"rows": [{"name": "A"}], "row_count": 1})  # no 'sources' key
    result = _run(deny_uncited_section(
        {"tool_name": "mcp__ms__generate_section",
         "tool_input": {"section": "Comp Set Overview", "evidence": [payload]}},
        "id", None,
    ))
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_governance_rejects_json_encoded_list_with_unsourced_row():
    """A JSON list where any row is missing a 'source' must be denied."""
    payload = json.dumps([
        {"name": "A", "source": "snapshot:foo"},
        {"name": "B"},  # no source
    ])
    result = _run(deny_uncited_section(
        {"tool_name": "mcp__ms__generate_section",
         "tool_input": {"section": "Comp Set Overview", "evidence": [payload]}},
        "id", None,
    ))
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_governance_rejects_non_json_string_entry():
    """A string evidence entry that isn't JSON-decodable is denied."""
    result = _run(deny_uncited_section(
        {"tool_name": "mcp__ms__generate_section",
         "tool_input": {"section": "X", "evidence": ["not json at all"]}},
        "id", None,
    ))
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
