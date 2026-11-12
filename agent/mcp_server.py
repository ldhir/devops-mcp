"""MCP server (stdio). Exposes the same tools the alert runner uses,
so an engineer can ask the same questions from Claude Code.

Run:  .venv/bin/python mcp_server.py
"""
import anyio
import mcp.types as types
from mcp.server import Server
from mcp.server.stdio import stdio_server

from tools import TOOLS, call_tool

server = Server("devops-mcp")


@server.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(name=name, description=spec["description"], inputSchema=spec["parameters"])
        for name, spec in TOOLS.items()
    ]


@server.call_tool()
async def run_tool(name: str, arguments: dict | None) -> list[types.TextContent]:
    text = await anyio.to_thread.run_sync(call_tool, name, arguments or {}, "mcp")
    return [types.TextContent(type="text", text=text)]


async def main() -> None:
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    anyio.run(main)
