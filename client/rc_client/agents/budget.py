"""How long the request being served may still wait for its agent.

The native apps give a request 20 s, and what the device does once the agent is there takes time
of its own: a Close may wait 8 s for a CLI to quit. So everything a request waits for before it
acts, its agent starting after a restart and the shared Codex daemon being reached, comes out of
one allowance, set as the request arrives and read wherever the request waits.
"""

from __future__ import annotations

import time
from contextvars import ContextVar

_until: ContextVar[float | None] = ContextVar("agent_wait_until", default=None)


def allow(seconds: float) -> None:
    """The request being served may wait `seconds` from now, in all, for its agent."""
    _until.set(time.monotonic() + seconds)


def left(otherwise: float) -> float:
    """What is left of the allowance; `otherwise` when nothing set one, as for a resume (A35)."""
    until = _until.get()
    return otherwise if until is None else max(0.0, until - time.monotonic())
