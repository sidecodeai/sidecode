"""The device's terminals: a login shell in a pseudo-terminal, streamed to one app (A38).

`TerminalManager` owns them and answers the six `terminal.*` requests; `Terminal`
is one shell. Nothing that travels through a terminal is ever logged.
"""

from __future__ import annotations

from .manager import DETACHED_KEEP_ALIVE_SECONDS, MAX_TERMINALS, TerminalManager
from .shell import Terminal

__all__ = ["DETACHED_KEEP_ALIVE_SECONDS", "MAX_TERMINALS", "Terminal", "TerminalManager"]
