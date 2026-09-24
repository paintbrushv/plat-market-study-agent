from agents.sdk.runner import build_options, SERVER_NAME


def test_allowed_tools_match_registered_tool_names():
    options, _ = build_options()
    expected = {
        f"mcp__{SERVER_NAME}__pull_comps",
        f"mcp__{SERVER_NAME}__normalize_rent_roll",
        f"mcp__{SERVER_NAME}__generate_section",
    }
    assert set(options.allowed_tools) == expected


def test_governance_hook_registered_under_pretooluse():
    options, _ = build_options()
    pre_hooks = options.hooks.get("PreToolUse", [])
    assert len(pre_hooks) == 1
    matcher = pre_hooks[0]
    assert getattr(matcher, "matcher", None) is None
    assert len(matcher.hooks) == 1


def test_server_name_constant_used_for_all_tool_identifiers():
    """If SERVER_NAME changes, every tool identifier must update — proves no hardcoding."""
    options, _ = build_options()
    for tool_id in options.allowed_tools:
        assert tool_id.startswith(f"mcp__{SERVER_NAME}__"), tool_id
