"""What a call reports its open time to, and what the answer can do to the call.

The call starts its meter when the Live API's session has started, ticks it with the seconds it has
been open, polls it for a reason to end, and stops it, once, with the seconds it was open however it
ended. Nothing in a call waits on a meter: what a meter has to pay for, it pays for behind the call.
"""

from __future__ import annotations

from typing import Any, Protocol

from .budget import DailyBudget


class CallMeter(Protocol):
    def start(self, call_id: str) -> None:
        """The session has started. Called once, first; ``call_id`` names the call."""
        ...

    def tick(self, seconds: float) -> None:
        """The call has been open ``seconds``. Never waits; a failed payment is not an error."""
        ...

    def verdict(self) -> str | None:
        """The reason the call has to end now (an end reason), or ``None`` while it may go on."""
        ...

    def stop(self, seconds: float) -> None:
        """The call was open ``seconds`` in all. Called once, whatever ended the call."""
        ...

    async def settled(self) -> None:
        """Return once what ``stop`` set going has an answer. The caller bounds the wait."""
        ...

    def record(self) -> dict[str, Any]:
        """The meter's numbers for the call's usage line: ``billed_seconds`` and ``billing``."""
        ...


class NoMeter:
    """A call nothing is paid for and nothing ends: what a call built without a meter has."""

    def start(self, call_id: str) -> None:
        return None

    def tick(self, seconds: float) -> None:
        return None

    def verdict(self) -> str | None:
        return None

    def stop(self, seconds: float) -> None:
        return None

    async def settled(self) -> None:
        return None

    def record(self) -> dict[str, Any]:
        return {"billed_seconds": 0, "billing": None}


class DailyMeter(NoMeter):
    """A call that bills nothing and counts against its account's call time for the day."""

    def __init__(self, budget: DailyBudget, username: str) -> None:
        self._budget = budget
        self._username = username

    def stop(self, seconds: float) -> None:
        self._budget.charge(self._username, seconds)
