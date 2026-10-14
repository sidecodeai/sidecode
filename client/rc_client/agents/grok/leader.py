"""Grok Build's leader: one shared backend per machine, joined as a client (A28).

`[cli] use_leader = true` makes every `grok` on the machine run its agent inside
one leader process instead of its own, and `agent agent --leader stdio` is an
ordinary client of that leader. Whichever client comes first starts it, the TUI
or this one, so there is nothing to hand-shake with beforehand: `config_ready`
reads the person's own configuration and says whether the next `grok` will join.

A `session/load` on a session a TUI has open joins that session rather than
opening a second copy. Every client then sees every update, a prompt from any of
them runs in the one conversation, and `session/cancel` and
`session/set_config_option` act for all. `session/close` is the exception: it
unloads the session for everyone, so it is sent in one case only — the person
closing a session no terminal is in, where ending it is the whole point (A39).
Letting go of a session without closing it is `detach`.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import tomllib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ...child_env import sanitized_child_env
from ...errors import RcError
from ...logging_setup import logger
from . import runtime
from .acp import GrokAgent

log = logger("rc_client.grok.leader")

SOCKET = "leader.sock"
SANDBOX_ENV = "GROK_SANDBOX"
# A profile that asks for no sandbox at all. Anything else refuses leader mode:
# the agent then runs in-process so its tools are never delegated elsewhere.
NO_SANDBOX = "off"
INFO_TIMEOUT = 15.0

NotificationHandler = Callable[[str, dict[str, Any]], Awaitable[None]]
ServerRequestHandler = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]
UnknownHandler = Callable[[str, str, dict[str, Any]], Awaitable[None]]
SessionsChangedHandler = Callable[[dict[str, Any]], Awaitable[None]]


def socket_path() -> Path:
    """Where `agent leader list|kill` and every TUI look for the leader."""
    return runtime.home() / SOCKET


def read_config() -> dict[str, Any]:
    """`~/.grok/config.toml`, or an empty table when there is none to read."""
    try:
        data = tomllib.loads(runtime.config_file().read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError):
        return {}
    return data


def _table(config: dict[str, Any], name: str) -> dict[str, Any]:
    found = config.get(name)
    return found if isinstance(found, dict) else {}


def use_leader(config: dict[str, Any] | None = None) -> bool:
    """Whether `[cli] use_leader` is on, which is off unless the person set it."""
    return _table(config if config is not None else read_config(), "cli").get("use_leader") is True


def sandbox_profile(config: dict[str, Any] | None = None) -> str:
    """The sandbox profile a new `grok` would start under, `off` for none.

    Three sources select one, and the environment outranks the configuration
    (`--sandbox` is per invocation and is nobody's business here). A non-`off`
    profile refuses leader mode outright.
    """
    from_env = os.environ.get(SANDBOX_ENV, "").strip()
    if from_env:
        return from_env
    table = _table(config if config is not None else read_config(), "sandbox")
    profile = table.get("profile")
    return profile.strip() if isinstance(profile, str) and profile.strip() else NO_SANDBOX


def sandboxed(config: dict[str, Any] | None = None) -> bool:
    return sandbox_profile(config) != NO_SANDBOX


def config_ready() -> bool:
    """Whether the next `grok` started on this machine will join the leader.

    This is `AgentInfo.attach_ready` for Grok (4.2): the person's own
    configuration rather than a handshake, because the leader only exists once
    some client has started it.
    """
    config = read_config()
    return use_leader(config) and not sandboxed(config)


@dataclass(slots=True)
class SessionRoute:
    """Where one session's notifications and requests are delivered."""

    on_notification: NotificationHandler
    on_request: ServerRequestHandler


class LeaderClient:
    """One ACP client of the machine's leader, shared by every attached session.

    Connecting starts a leader when none is running, which is exactly what a TUI
    does, so it is never a side effect worth avoiding.
    """

    def __init__(
        self,
        binary: str,
        *,
        cwd: str | None = None,
        on_unknown: UnknownHandler | None = None,
        on_sessions_changed: SessionsChangedHandler | None = None,
    ) -> None:
        self._binary = binary
        self._cwd = cwd or str(Path.home())
        self._on_unknown = on_unknown
        self._on_sessions_changed = on_sessions_changed
        self._agent: GrokAgent | None = None
        self._routes: dict[str, SessionRoute] = {}
        self._version: str | None = None
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------- lifecycle

    @property
    def connected(self) -> bool:
        return self._agent is not None and self._agent.alive

    @property
    def version(self) -> str | None:
        """The leader's own version, which drifts when Grok is updated under it."""
        return self._version

    async def connect(self) -> bool:
        """Join the leader, starting one if none answers. False means it did not."""
        async with self._lock:
            if self.connected:
                return True
            await self._drop()
            agent = GrokAgent(
                self._binary,
                cwd=self._cwd,
                leader=True,
                env=sanitized_child_env(),
                on_notification=self._notification,
                on_request=self._request,
            )
            try:
                result = await agent.start()
            except Exception as exc:
                log.info("the grok leader did not answer", error=str(exc)[:200])
                with contextlib.suppress(Exception):
                    await agent.close()
                return False
            self._agent = agent
            meta = result.get("_meta")
            version = meta.get("agentVersion") if isinstance(meta, dict) else None
            self._version = version if isinstance(version, str) and version else None
            log.info("joined the grok leader", version=self._version or "unknown")
            return True

    async def close(self) -> None:
        """Leave the leader. It stays up, as it does when a TUI quits."""
        async with self._lock:
            self._routes.clear()
            await self._drop()

    async def _drop(self) -> None:
        agent, self._agent = self._agent, None
        if agent is not None:
            with contextlib.suppress(Exception):
                await agent.close()

    # ---------------------------------------------------------------- routing

    def attach(self, session_id: str, route: SessionRoute) -> None:
        self._routes[session_id] = route

    def detach(self, session_id: str) -> None:
        """Stop routing one session, leaving it loaded for whoever else is in it.

        `session/close` is the other ending, and unloads the session for every
        client of the leader; only a close of a session no terminal is in sends
        it (A39), and it goes out as an ordinary `request`.
        """
        self._routes.pop(session_id, None)

    def routed(self, session_id: str) -> bool:
        return session_id in self._routes

    def sessions(self) -> list[str]:
        return list(self._routes)

    # ----------------------------------------------------------------- calls

    @property
    def _live(self) -> GrokAgent:
        agent = self._agent
        if agent is None or not agent.alive:
            raise RcError("agent_unavailable", "the grok leader is not connected")
        return agent

    async def request(
        self, method: str, params: dict[str, Any], timeout: float | None = None
    ) -> dict[str, Any]:
        return await self._live.request(method, params, timeout=timeout)

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        await self._live.notify(method, params)

    async def respond_error(self, request_id: Any, code: int, message: str) -> None:
        await self._live.respond_error(request_id, code, message)

    async def session_info(self, session_id: str) -> dict[str, Any]:
        """`_x.ai/session/info`: a rich object when loaded, `{}` when it is not.

        This is the whole test for "a leader-mode TUI is in this session": the
        registry says a process holds it, and this says the leader has it.
        """
        try:
            result = await self.request(
                "_x.ai/session/info", {"sessionId": session_id}, timeout=INFO_TIMEOUT
            )
        except RcError as exc:
            log.warning("grok session info failed", error=exc.message[:200])
            return {}
        inner = result.get("result")
        return inner if isinstance(inner, dict) and inner else result

    async def titles(self, cwd: str) -> dict[str, str]:
        """The title the leader has for each session in one directory."""
        try:
            result = await self.request("_x.ai/session/list", {"cwd": cwd}, timeout=INFO_TIMEOUT)
        except RcError as exc:
            log.warning("grok session list failed", error=exc.message[:200])
            return {}
        inner = result.get("result")
        payload = inner if isinstance(inner, dict) else result
        found: dict[str, str] = {}
        for entry in payload.get("sessions") or []:
            if not isinstance(entry, dict):
                continue
            session_id = entry.get("sessionId")
            title = entry.get("title") or entry.get("summary")
            if isinstance(session_id, str) and isinstance(title, str) and title.strip():
                found[session_id] = title.strip()
        return found

    # ---------------------------------------------------------------- inbound

    async def _notification(self, method: str, params: dict[str, Any]) -> None:
        if method == "_x.ai/sessions/changed":
            if self._on_sessions_changed is not None:
                await self._on_sessions_changed(params)
            return
        session_id = str(params.get("sessionId") or "")
        if not session_id:
            return
        route = self._routes.get(session_id)
        if route is not None:
            await route.on_notification(method, params)
            return
        if self._on_unknown is not None:
            await self._on_unknown(session_id, method, params)

    async def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        session_id = str(params.get("sessionId") or "")
        route = self._routes.get(session_id)
        if route is None:
            # Nobody here is in that session, so the terminal answers its own
            # dialog: refusing is what hands the prompt back to it.
            raise RcError("conflict", "no attached session for this grok session")
        return await route.on_request(method, params)
