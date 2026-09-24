from claude_agent_sdk import (
    ClaudeAgentOptions, ClaudeSDKClient, HookMatcher,
    create_sdk_mcp_server, AssistantMessage, TextBlock, ToolUseBlock,
)
from .tools import pull_comps_tool, normalize_rent_roll_tool, generate_section_tool
from .governance import deny_uncited_section


SERVER_NAME = "ms"


def build_options() -> tuple[ClaudeAgentOptions, object]:
    """Construct the SDK options + server. Exposed for unit testing of tool/hook wiring."""
    server = create_sdk_mcp_server(name=SERVER_NAME, version="1.0.0",
        tools=[pull_comps_tool, normalize_rent_roll_tool, generate_section_tool])
    options = ClaudeAgentOptions(
        mcp_servers={SERVER_NAME: server},
        allowed_tools=[
            f"mcp__{SERVER_NAME}__pull_comps",
            f"mcp__{SERVER_NAME}__normalize_rent_roll",
            f"mcp__{SERVER_NAME}__generate_section",
        ],
        hooks={"PreToolUse": [HookMatcher(matcher=None, hooks=[deny_uncited_section])]},
    )
    return options, server


async def run_comp_set_overview(metro: str) -> str:
    options, _server = build_options()
    output: list[str] = []
    async with ClaudeSDKClient(options=options) as client:
        await client.query(
            f"For metro '{metro}', call mcp__ms__pull_comps to get a 5-comp set. "
            f"The result will be a JSON-encoded payload with a 'rows' field and a "
            f"'sources' field. Pass the SAME PAYLOAD STRING as the single evidence "
            f"entry to mcp__ms__generate_section with section='Comp Set Overview' "
            f"and evidence=[<the json string>]. Do not fabricate or summarize "
            f"the rows — pass the payload through verbatim. Refuse to generate "
            f"without evidence."
        )
        async for msg in client.receive_response():
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        output.append(block.text)
                    elif isinstance(block, ToolUseBlock):
                        output.append(f"[tool] {block.name}({block.input})")
    return "\n".join(output)
