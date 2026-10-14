"""Amendment A11: Codex through the shared `codex app-server` daemon.

Codex 0.153 and later run every terminal `codex` TUI inside one local app-server
daemon whose control socket speaks JSON-RPC over WebSocket-over-AF_UNIX. A
second client that resumes a thread there sees the same stream as the TUI, can
start, steer and interrupt turns, change settings and answer approvals. This
package is that second client and the session model built on it.
"""

from __future__ import annotations

from .transport import DAEMON_SOCKET, socket_path

__all__ = ["DAEMON_SOCKET", "socket_path"]
