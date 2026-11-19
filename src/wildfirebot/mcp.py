"""Model Context Protocol entry point.

Milestone 3 will serve these tools over stdio. The handlers already live in
`wildfirebot.tools` so the server does not grow a second forecast implementation.
"""

from __future__ import annotations

from wildfirebot.tools import TOOLS, call_tool

__all__ = ["TOOLS", "call_tool", "list_tools"]


def list_tools() -> list[dict]:
    """Return the MCP tool list a future `tools/list` response will send."""
    return TOOLS
