"""Investigate one alert with Claude Code instead of the OpenAI API.

Claude Code is the model and the loop; the tools are this project's MCP server, so the
allowlist, masking and audit log are the same as in the alert runner. No OpenAI key is used.

    python claude_code_investigate.py --mcp-config mcp.json \\
        "Disk almost full: /mnt/mongo-data on lab-host-1 is over 85% full"

This prints the write-up. It does not post to Slack, run the second pass or record cost;
those live in runner.py, which only speaks the OpenAI API today.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import datetime, timezone

from prompts import SYSTEM

SERVER = "devops-mcp"   # the server name used in the MCP config file


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("alert", help="the alert text, as it would appear in Slack")
    ap.add_argument("--mcp-config", required=True, help="JSON file that tells Claude Code how to start mcp_server.py")
    ap.add_argument("--model", default="", help="optional Claude Code model name or alias")
    args = ap.parse_args()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    prompt = (f"{SYSTEM}\n\nThe tools are the {SERVER} MCP tools. Use only those.\n\n"
              f"Time now: {now}\nAlert:\n{args.alert}")
    cmd = ["claude", "-p", prompt, "--mcp-config", args.mcp_config, "--strict-mcp-config",
           "--allowedTools", f"mcp__{SERVER}"]
    if args.model:
        cmd += ["--model", args.model]
    return subprocess.run(cmd, stdin=subprocess.DEVNULL).returncode


if __name__ == "__main__":
    sys.exit(main())
