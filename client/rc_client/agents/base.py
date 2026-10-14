"""The interface every agent adapter implements, plus the translator output type.

Translators are pure: they turn one agent message into a list of `Emit`s, which
the adapter hands to the session channel. Keeping them side-effect free is what
makes them testable against recorded fixtures without running an agent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from ..models import UNSET, Command, SpeedSetting

# Every agent that drops the earlier part of a conversation to make room says
# the same thing to the apps, so the line lives here rather than once per
# translator: a reader should not have to learn two wordings for one event.
COMPACTION_NOTICE = "Context was compacted; earlier turns are summarised."

TOOL_KINDS = (
    "shell",
    "read",
    "edit",
    "write",
    "search",
    "web",
    "mcp",
    "subagent",
    "todo",
    "other",
)


@dataclass(slots=True)
class Emit:
    """One event to publish. `delta` marks coalesced streaming appends."""

    kind: str
    fields: dict[str, Any] = field(default_factory=dict)
    delta: bool = False


@dataclass(slots=True)
class SendResult:
    accepted: str
    queued_id: str | None = None


@runtime_checkable
class SessionRunner(Protocol):
    """A live agent session driven by the daemon."""

    agent: str

    async def start(self) -> None: ...

    async def send(
        self,
        text: str,
        attachments: list[dict[str, Any]] | None = None,
        source: str = "remote",
        block_id: str | None = None,
    ) -> None:
        """Start a turn. ``source`` is the protocol trigger: `remote`, `terminal` or `queue`.

        ``block_id`` reuses an existing bubble instead of opening a new one, so a
        message already shown as pending is replaced in place when it goes out.
        Amendment A12: for a message an app sent, it is that request's id.
        """
        ...

    async def interrupt(self) -> bool: ...

    async def approve(self, request_id: str, option_id: str, message: str | None) -> bool: ...

    async def answer(self, request_id: str, answers: dict[str, Any]) -> bool: ...

    async def apply_settings(
        self,
        model: str | None,
        permission_mode: str | None,
        effort: str | None,
        speed: SpeedSetting = UNSET,
    ) -> None:
        """Change what the request named. `None` leaves a setting alone.

        `speed` is the exception: `None` is the agent's standard speed and
        `UNSET` is a request that never mentioned it (amendment A21).
        """
        ...

    async def close(self) -> None:
        """Let go of the session here. What runs elsewhere runs on."""
        ...

    async def shutdown(self) -> None:
        """End the session for good, then let go of it (amendment A39).

        `close` is the device stepping back from something that may not be
        only its own — a Codex thread a terminal could still be in, a Grok
        session the leader holds for every client. `shutdown` is the person
        closing the session: the turn is stopped and what the device holds for
        the agent is ended before `close` runs, so nothing of the session is
        left running on the machine.
        """
        ...

    @property
    def busy(self) -> bool: ...

    @property
    def supports_steer(self) -> bool: ...

    async def steer(self, text: str, block_id: str | None = None) -> bool: ...

    async def commands(self) -> list[Command]:
        """The slash commands this session offers right now; empty when none (A27)."""
        ...

    async def command(self, name: str, argument: str | None, block_id: str) -> None:
        """Run one slash command on an idle session (A27).

        The runner echoes it as a `user_message` under ``block_id`` with the
        text the user typed (`/name argument`) and reports the outcome as
        events. Raises `RcError("not_found")` for a name it does not offer.
        """
        ...
