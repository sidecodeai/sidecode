"""The prompts and the turn of one Antigravity session, as plain records (A64).

A message is one `session/prompt`, and the turn ends when the last prompt sent returns: a message
sent while a turn runs is another prompt, which `agy` takes over the turn with, answering the
earlier one at once. A turn `agy` begins by itself, after its prompt has answered, has no prompt
at all. `usage` is what the session has spent through the device: `agy`'s token counts for every
prompt, summed, beside the context in use as `agy` last reported it (4.7).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from ...attachments import Attachment
from ...models import now_ms

# The token counts `agy` reports for a prompt, as `usage` carries them.
TOKENS = (
    ("inputTokens", "input_tokens"),
    ("outputTokens", "output_tokens"),
    ("totalTokens", "total_tokens"),
)
# The JSON-RPC errors that say a request was refused as it stood, so `agy` never read the prompt.
REFUSED = frozenset({-32600, -32601, -32602})


@dataclass(slots=True)
class Prompt:
    """One message on its way to `agy`, and what became of it."""

    text: str
    block_id: str
    source: str
    blocks: list[dict[str, Any]]
    shown: list[dict[str, Any]] = field(default_factory=list)
    written: list[Attachment] = field(default_factory=list)
    # A message sent into a running turn, whose bubble waits for `agy` to take it (A14).
    steer: bool = False
    announced: bool = False
    task: asyncio.Task[None] | None = None
    # For a steer: whether `agy` took it over the turn.
    taken: asyncio.Future[bool] = field(
        default_factory=lambda: asyncio.get_running_loop().create_future()
    )

    def settle(self, taken: bool) -> None:
        if not self.taken.done():
            self.taken.set_result(taken)


@dataclass(slots=True)
class Turn:
    """The turn under way: when it began, whether `agy` began it, and its words for A35."""

    wake: bool = False
    started_at: int = field(default_factory=now_ms)
    text: str = ""
    error: str = ""
    # Whether a turn `agy` began by itself has said its first words yet.
    spoken: bool = False

    @property
    def duration(self) -> int:
        return max(0, now_ms() - self.started_at)


def add_usage(totals: dict[str, Any], usage: Any) -> None:
    """Add one prompt's token counts to the session's."""
    if not isinstance(usage, dict):
        return
    for theirs, ours in TOKENS:
        value = usage.get(theirs)
        if isinstance(value, int) and not isinstance(value, bool):
            totals[ours] = int(totals.get(ours) or 0) + value


def note_context(totals: dict[str, Any], update: dict[str, Any]) -> None:
    """The context in use and its size, as one `usage_update` reports them."""
    for theirs, ours in (("used", "context_used"), ("size", "context_window")):
        value = update.get(theirs)
        if isinstance(value, int) and not isinstance(value, bool):
            totals[ours] = value
