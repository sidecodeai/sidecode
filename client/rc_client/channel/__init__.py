"""Attach the device to an interactive Claude Code session through a channel.

`rc-client channel` runs as a stdio MCP server that the CLI spawns. It declares
the `claude/channel` capabilities, relays permission prompts to the daemon over
a Unix socket and injects the messages the daemon hands back.
"""

from __future__ import annotations

__all__ = ["paths", "shim", "wire"]
