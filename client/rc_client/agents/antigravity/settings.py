"""Setting a session's model, thinking level and mode in `agy` (A64, 4.3).

The model goes first, because `agy` puts the thinking level back to the new model's own default
when it changes; then the level the session asked for, kept while the model offers it and otherwise
the nearest one below; then the mode. Each answer is `agy`'s whole option list, which says what the
session now runs with, and that is what the apps are told (A17). A session in the device's own
Bypass permissions runs `agy` in `accept-edits`, and is said to be in Bypass permissions while `agy`
runs it so (A67).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ...errors import ErrorCode, RcError
from ...sessions.channel import SessionChannel
from . import errors
from .acp import AgyConnection, AgyError
from .options import BYPASS, MODE, MODEL, THOUGHT_LEVEL, Running, agy_mode, clamp, running
from .turns import REFUSED


@dataclass(slots=True)
class Wanted:
    """What a session asked to run with; None leaves a setting as `agy` has it."""

    model: str | None = None
    effort: str | None = None
    mode: str | None = None


async def apply(agy: AgyConnection, session_id: str, now: Running, wanted: Wanted) -> Running:
    """Set what `wanted` names and `now` does not already run with; what `agy` then runs."""
    if wanted.model and wanted.model != now.model:
        now = await _option(agy, session_id, now, MODEL, wanted.model)
    level = clamp(wanted.effort, now.levels)
    if level is not None and level != now.effort:
        now = await _option(agy, session_id, now, THOUGHT_LEVEL, level)
    mode = agy_mode(wanted.mode)
    if mode and mode != now.mode:
        now = await _mode(agy, session_id, now, mode)
    return now


async def _option(
    agy: AgyConnection, session_id: str, now: Running, option: str, value: str
) -> Running:
    answer = await agy.request(
        "session/set_config_option", {"sessionId": session_id, "configId": option, "value": value}
    )
    return running(answer, now)


async def _mode(agy: AgyConnection, session_id: str, now: Running, mode: str) -> Running:
    """`mode` as a session option when `agy` lists it as one, else through `session/set_mode`."""
    if now.mode_option:
        return await _option(agy, session_id, now, MODE, mode)
    await agy.request("session/set_mode", {"sessionId": session_id, "modeId": mode})
    after = running({}, now)
    after.mode = mode
    return after


async def publish(channel: SessionChannel, now: Running, mode: str | None) -> None:
    """What the session runs with, as `meta` (A17); no levels means no effort."""
    fields: dict[str, Any] = {"effort": now.effort if now.levels else None}
    if now.model:
        fields["model"] = now.model
    if mode:
        fields["permission_mode"] = mode
    await channel.set_meta(**fields)


class SessionSettings:
    """What one session asked to run with, and what `agy` runs it with now."""

    def __init__(self, channel: SessionChannel, wanted: Wanted) -> None:
        self.channel = channel
        self.wanted = wanted
        self.now = Running()

    def ask(self, model: str | None, mode: str | None, effort: str | None) -> None:
        """What `session.set` named; a setting it left out stays as it was asked before."""
        if model is not None:
            self.wanted.model = model
        if effort is not None:
            self.wanted.effort = effort
        if mode is not None:
            self.wanted.mode = mode

    async def start(self, agy: AgyConnection, session_id: str, started: Running) -> None:
        """A new process starts on the account's defaults: the session's own are set again."""
        self.now = await apply(agy, session_id, started, self.wanted)

    async def change(self, agy: AgyConnection, session_id: str) -> None:
        """Set what the session now asks for, refused in `agy`'s words when `agy` refuses it."""
        try:
            self.now = await apply(agy, session_id, self.now, self.wanted)
        except AgyError as exc:
            code: ErrorCode = "bad_request" if exc.rpc_code in REFUSED else "agent_unavailable"
            raise RcError(code, errors.words(exc.message)) from exc
        await self.publish()

    async def heard(self, kind: str, update: dict[str, Any]) -> None:
        """`agy` says what options the session runs with now."""
        if kind == "config_option_update":
            self.now = running(update, self.now)
        else:
            mode = update.get("currentModeId")
            if not isinstance(mode, str) or not mode:
                return
            self.now.mode = mode
        await self.publish()

    @property
    def bypassing(self) -> bool:
        """Whether the device approves the session's tools itself (A67).

        Bypass permissions, while `agy` runs the session in the mode it runs that in; asking for
        another mode ends it at once.
        """
        return self.wanted.mode == BYPASS.id and self.now.mode == agy_mode(BYPASS.id)

    async def publish(self) -> None:
        await publish(self.channel, self.now, BYPASS.id if self.bypassing else self.now.mode)
