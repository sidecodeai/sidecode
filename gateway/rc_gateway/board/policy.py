"""Who may have boards kept, and on whose model: the rules around an update, behind one interface.

``BoardService`` asks a ``BoardPolicy`` two things. ``quick`` answers from what the gateway holds
in memory and on its own disk, so a `board.get` reply never waits on anyone else; ``admit`` is
asked before any update reads the device or calls a model, and returns the model the update calls,
or refuses with one of the four reasons a deployment's policy may give. A self-hosted gateway uses
``OperatorPolicy``: every account, the operator's own model, the operator pays. A gateway that bills
its model calls hands the service a policy of its own; this package imports none of it.
"""

from __future__ import annotations

from typing import Protocol

from .llm import BoardModel

UNSUPPORTED = "unsupported"
SUBSCRIPTION_REQUIRED = "subscription_required"
QUOTA_EXCEEDED = "quota_exceeded"
BILLING_NOT_CONFIGURED = "billing_not_configured"
#: The `unavailable` reasons of `BoardStatus`: the interface a deployment's policy fills in.
REASONS = frozenset({UNSUPPORTED, SUBSCRIPTION_REQUIRED, QUOTA_EXCEEDED, BILLING_NOT_CONFIGURED})


class BoardRefusal(Exception):
    """The policy will not have this account's boards updated now, for ``reason``."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason if reason in REASONS else UNSUPPORTED


class BoardPolicy(Protocol):
    async def quick(self, username: str) -> str | None:
        """The reason the account's boards cannot be updated, from local state alone, or None."""
        ...

    async def admit(self, username: str) -> BoardModel:
        """The model one update of the account's board calls, or ``BoardRefusal``.

        Asked before the device is read. A call the model then refuses over the account's
        allowance raises ``BoardRefusal`` too; any other failure is ``ModelFailure``.
        """
        ...


class OperatorPolicy:
    """The self-hosted rule: every account may have boards, on the operator's model."""

    def __init__(self, model: BoardModel) -> None:
        self.model = model

    async def quick(self, username: str) -> str | None:
        return None

    async def admit(self, username: str) -> BoardModel:
        return self.model

    async def close(self) -> None:
        closer = getattr(self.model, "close", None)
        if closer is not None:
            await closer()
