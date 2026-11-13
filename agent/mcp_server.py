"""MCP server (stdio). Exposes the same read tools the alert runner uses,
so an engineer can ask the same questions from Claude Code or any MCP client.

Run:  .venv/bin/python mcp_server.py        (needs the mcp package, version 2 or later)
"""
import anyio
import mcp.types as types
from mcp.server import Server
from mcp.server.stdio import stdio_server

from tools import TOOLS, call_tool

# Posting to Slack is the alert runner's job. An MCP client only gets the read tools.
HIDDEN = {"post_update"}


async def list_tools(ctx, params) -> types.ListToolsResult:
    return types.ListToolsResult(tools=[
        types.Tool(name=name, description=spec["description"], inputSchema=spec["parameters"])
        for name, spec in TOOLS.items()
        if name not in HIDDEN
    ])


async def run_tool(ctx, params) -> types.CallToolResult:
    if params.name in HIDDEN:
        text = '{"error": "not available over MCP"}'
    else:
        # Same choke point as the runner: allowlist, masking, audit log.
        text = await anyio.to_thread.run_sync(call_tool, params.name, params.arguments or {}, "mcp")
    return types.CallToolResult(content=[types.TextContent(type="text", text=text)])


server = Server("devops-mcp", on_list_tools=list_tools, on_call_tool=run_tool)


async def main() -> None:
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    anyio.run(main)
