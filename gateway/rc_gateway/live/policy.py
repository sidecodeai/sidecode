"""Who may call, and what a call costs: the rules around a call, behind one interface.

``LiveService`` asks a ``LivePolicy`` what to tell an account about the assistant (``status``) and
whether it may start a call now (``admit``, which refuses with a ``LiveError`` whose code is the
reason). The admission carries the meter the call reports its open time to (``meter.py``). A
self-hosted gateway uses ``AllowlistPolicy``: the accounts in ``LIVE_ALLOWED_USERS``, each held to
``LIVE_DAILY_SECONDS`` of call time a day, nothing billed. A gateway that bills its calls hands the
service a policy of its own; this package imports none of it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from .budget import DailyBudget
from .config import LiveConfig
from .errors import LiveError
from .meter import CallMeter, DailyMeter


@dataclass(frozen=True)
class LiveStatus:
    """What the assistant is for one account.

    ``enabled`` says whether the entry is shown at all; ``available`` whether a call can be
    started now, and ``reason``, while it cannot, why (the reason a refused call would end with).
    """

    enabled: bool
    available: bool
    reason: str | None

    def view(self) -> dict[str, Any]:
        return {"enabled": self.enabled, "available": self.available, "reason": self.reason}


@dataclass(frozen=True)
class Admission:
    """An account let in: the meter its call reports to, and the cap on how long the call may be."""

    meter: CallMeter
    #: Seconds of call time the account has left today, or ``None`` when there is no daily cap.
    budget_seconds: float | None = None


class LivePolicy(Protocol):
    async def status(self, username: str) -> LiveStatus:
        """What to tell the account. A refusal ``admit`` would give is ``available: false``."""
        ...

    async def admit(self, username: str) -> Admission:
        """Let the account start a call, or raise ``LiveError`` with the reason it may not.

        Asked before anything is dialled, and it costs nothing to be refused: the meter it returns
        does nothing until a call starts it.
        """
        ...


class AllowlistPolicy:
    """The self-hosted rules: only the listed accounts, and a daily cap on each one's call time."""

    def __init__(self, config: LiveConfig, budget: DailyBudget | None = None) -> None:
        self._allowed = config.allowed_users
        self._budget = budget if budget is not None else DailyBudget(config.daily_seconds)

    async def status(self, username: str) -> LiveStatus:
        if username in self._allowed:
            return LiveStatus(enabled=True, available=True, reason=None)
        return LiveStatus(enabled=False, available=False, reason="not_allowed")

    async def admit(self, username: str) -> Admission:
        if username not in self._allowed:
            raise LiveError("not_allowed", "the assistant is not enabled for you")
        left = self._budget.remaining(username)
        if left <= 0:
            raise LiveError("daily_limit", "today's voice time has been used")
        return Admission(DailyMeter(self._budget, username), budget_seconds=left)
