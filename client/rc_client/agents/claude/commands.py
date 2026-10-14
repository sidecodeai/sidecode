"""The one slash command a Claude session offers an app (A27 through A40).

Claude Code has a page of commands, and almost none of them can be honoured
from a phone: the settings ones are `session.set`, the lifecycle ones are
frames of their own, and the rest change a terminal the apps cannot see. What
is left is `/compact`, which changes the conversation itself — so that is the
whole list, the same on a session the device runs and on one it types into.
"""

from __future__ import annotations

from ...models import Command

COMPACT = Command(
    "compact",
    "Summarise the conversation so far to free context",
    group="Built-in",
)

COMMANDS: tuple[Command, ...] = (COMPACT,)
