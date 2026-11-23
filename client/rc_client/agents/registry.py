"""The agent registry: one package per agent, one list of ids.

Every agent the device can drive lives in `rc_client/agents/<id>/` and exposes a
`plugin` module with `AGENT`, `detect` and `build_runner`. `AGENT_IDS` below is
the only place an id is written down, so teaching the device a new agent is a
new package plus a name in that tuple — nothing in the daemon, the hub or the
CLI changes.

Plugin modules are imported on first use rather than at import time, so a broken
adapter cannot stop the daemon from starting with the agents that do work.
"""

from __future__ import annotations

import asyncio
import importlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, Any, Protocol, cast

from ..errors import RcError
from ..models import AgentInfo, Command, Session
from .base import SessionRunner
from .detection import Detections

if TYPE_CHECKING:  # pragma: no cover - imported for types only
    from ..config import MirrorConfig
    from ..sessions.channel import SessionChannel
    from ..sessions.hub import SessionEntry, SessionHub
    from ..sessions.shared import SharedControl
    from .codex.daemon.service import CodexDaemonService
    from .grok.service import GrokLeaderService
    from .pi.service import PiExtensionService

# Detection order, and the order `hello` reports agents in.
AGENT_IDS = ("claude", "codex", "grok", "pi", "antigravity")

TurnEndCallback = Callable[[], Awaitable[None]]
SessionIdCallback = Callable[[str], Awaitable[None]]
# Reads `account/rateLimits/read` on the shared Codex daemon (A33).
RateLimitsReader = Callable[[], Awaitable[dict[str, Any]]]
# Reads `_x.ai/billing` on the Grok leader this device is connected to (A53).
BillingReader = Callable[[], Awaitable[dict[str, Any]]]


@dataclass(slots=True)
class DetectContext:
    """What a detector needs beyond the filesystem it can read for itself."""

    # Whether the shared Codex app-server answered a handshake (A11). `None`
    # asks the Codex plugin to find out for itself.
    codex_daemon_ready: bool | None = None
    # Whether this detection answers `device.agents`, the one frame that carries
    # each account's rate-limit windows read fresh (A33). `hello` and
    # `agents.updated` never do, so they cost no network call.
    limits: bool = False
    # How to ask the shared Codex daemon for its account's windows; absent when
    # there is no daemon connection to ask.
    codex_rate_limits: RateLimitsReader | None = None
    # How to ask the Grok leader for its account's window (A53); absent when
    # quota is not wanted. It says itself when leader mode is off.
    grok_billing: BillingReader | None = None
    # How this device dials out, as `config.toml` keeps it: `""` is direct. The one call a
    # detection makes over the network, the Anthropic quota read, dials the same way (A33).
    proxy: str = ""


@dataclass(slots=True)
class RunnerSpec:
    """Everything an adapter needs to build one live session."""

    entry: SessionEntry
    info: AgentInfo
    resume: str | None
    on_turn_end: TurnEndCallback
    on_session_id: SessionIdCallback
    codex_daemon: CodexDaemonService | None = None
    # Set once the machine's Grok leader is in play (A28); absent means the
    # private-child path.
    grok_leader: GrokLeaderService | None = None

    @property
    def channel(self) -> SessionChannel:
        return self.entry.channel

    @property
    def session(self) -> Session:
        return self.entry.session


@dataclass(slots=True)
class RenameSpec:
    """Everything an adapter needs to rename one session inside its agent (A55).

    `name` has been through `sessions.rename.normalise`: it is not empty, holds
    no control or separator character, and is at most 60 code points, so it is
    safe to type, write and send as it is. The hub holds `entry.lock` for as
    long as the hook runs; a hook never takes it.
    """

    entry: SessionEntry
    info: AgentInfo
    name: str
    shared: SharedControl
    codex_daemon: CodexDaemonService | None = None
    grok_leader: GrokLeaderService | None = None
    pi_extensions: PiExtensionService | None = None

    @property
    def channel(self) -> SessionChannel:
        return self.entry.channel

    @property
    def session(self) -> Session:
        return self.entry.session


# `async def rename(spec) -> str` on a plugin module, when the agent can be renamed.
RenameHook = Callable[[RenameSpec], Awaitable[str]]


@dataclass(slots=True)
class CloseSpec:
    """Everything an adapter needs to end the CLI a terminal runs a session in (A61).

    `deadline` is on the event loop's clock: the CLI has to be gone by then.
    `process_ended` is how a wait for a process's end is made, the hub's own,
    which a test can replace. The hub holds `entry.lock` for as long as the hook
    runs; a hook never takes it.
    """

    entry: SessionEntry
    shared: SharedControl
    deadline: float
    process_ended: Callable[[int, float], Awaitable[bool]]
    codex_daemon: CodexDaemonService | None = None
    grok_leader: GrokLeaderService | None = None

    @property
    def session(self) -> Session:
        return self.entry.session


# `async def close_terminal(spec) -> bool` on a plugin module whose agent lists
# `close_terminal`: True once the CLI is gone, False when it is still there at the
# deadline; `RcError("unsupported")`, with nothing done, when it cannot be ended.
CloseHook = Callable[[CloseSpec], Awaitable[bool]]


class AgentPlugin(Protocol):
    """The shape every `rc_client/agents/<id>/plugin.py` satisfies."""

    AGENT: str
    detect: Callable[[DetectContext], Awaitable[AgentInfo]]
    build_runner: Callable[[RunnerSpec], Awaitable[SessionRunner]]


# Re-detects one agent and says the agents again when that changed what they offer.
Refresh = Callable[[], Awaitable[None]]


class AgentService(Protocol):
    """What an agent keeps running beside its sessions, from its start to the device's stop.

    A plugin with one exposes `def service() -> AgentService`; the daemon starts it once the agent
    has started, with the hub, a way to have the agent detected again and the bounds `[mirror]`
    sets on what the device takes on from the agent's own history, and stops it with itself.
    """

    async def start(self, hub: SessionHub, refresh: Refresh, limits: MirrorConfig) -> None: ...

    async def stop(self) -> None: ...


_loaded: dict[str, AgentPlugin] = {}
# What the device knows of its agents between two detections (`detection.py`).
detections = Detections()


def plugin(agent: str) -> AgentPlugin:
    """The adapter for `agent`, or the error the session frames answer with.

    Its first import brings in the agent's SDK, which takes seconds on a cold disk: anything on
    the event loop asks `load` instead.
    """
    if agent not in AGENT_IDS:
        raise RcError("unsupported", f"no adapter for agent {agent}")
    cached = _loaded.get(agent)
    if cached is None:
        cached = cast(AgentPlugin, importlib.import_module(f"{__package__}.{agent}.plugin"))
        _loaded[agent] = cached
    return cached


async def detect_all(context: DetectContext | None = None) -> list[AgentInfo]:
    """Ask every agent what it offers, concurrently, in `AGENT_IDS` order.

    A request for quota (`context.limits`) does not wait more than `AGENT_TIMEOUT` for an agent
    that has answered before, and an agent that fails is answered with what it last said, or left
    out of the list: neither holds up or fails the rest (`detection.py`).
    """
    ready = context or DetectContext()
    answers = await asyncio.gather(*(detect_one(agent, ready) for agent in AGENT_IDS))
    return [info for info in answers if info is not None]


async def load(agent: str) -> AgentPlugin:
    """The adapter for `agent`, imported on a worker thread the first time anything needs it.

    On the event loop that import froze everything the device was doing, its gateway link
    included, for as long as the SDK took to load. An adapter counts as loaded only once its
    import has finished, so a second caller in the meantime waits on a thread of its own too.
    """
    if agent in _loaded:
        return plugin(agent)
    return await asyncio.to_thread(plugin, agent)


async def detect_one(agent: str, context: DetectContext) -> AgentInfo | None:
    """What one agent offers, as `detect_all` asks it; None when it has no answer."""
    return await detections.detect(agent, context, partial(_ask, agent, context))


async def _ask(agent: str, context: DetectContext) -> AgentInfo:
    return await (await load(agent)).detect(context)


async def runner_for(agent: str, spec: RunnerSpec) -> SessionRunner:
    return await (await load(agent)).build_runner(spec)


async def service_for(agent: str) -> AgentService | None:
    """The service `agent`'s plugin keeps beside its sessions, or None when it keeps none."""
    hook = getattr(await load(agent), "service", None)
    return cast("AgentService", hook()) if hook is not None else None


def terminal_conflict(agent: str) -> str | None:
    """The words an agent's plugin refuses a message to a session a terminal holds with, if its own.

    A plugin may set `TERMINAL_CONFLICT` when the hub's own sentence would say more than the device
    knows. Only a plugin already loaded is asked: a session of an agent that never loaded has no
    terminal of its to be held by.
    """
    words = getattr(_loaded.get(agent), "TERMINAL_CONFLICT", None)
    return words if isinstance(words, str) and words else None


async def offline_commands(agent: str, session: Session) -> list[Command]:
    """What a session with no live process can still be said to offer (A27).

    A plugin may expose `async def commands(session) -> list[Command]` for the
    list it knows without starting anything: Codex's fixed table, pi's prompt
    templates and skills on disk, the list Grok last advertised. Without it the
    answer is empty until the session is resumed.
    """
    offline = getattr(await load(agent), "commands", None)
    if offline is None:
        return []
    return list(await offline(session))


async def close_terminal_in_agent(agent: str, spec: CloseSpec) -> bool:
    """End the CLI a terminal runs a session in, and say whether it ended in time (A61)."""
    hook = cast("CloseHook | None", getattr(await load(agent), "close_terminal", None))
    if hook is None:
        raise RcError("unsupported", f"{agent} sessions cannot be closed from here")
    return await hook(spec)


async def rename_in_agent(agent: str, spec: RenameSpec) -> str:
    """Rename a session inside its agent, and return the name the agent now holds (A55).

    A plugin exposes `async def rename(spec: RenameSpec) -> str` and picks its
    own route from the session's state: the process the device drives, the
    terminal it is attached to, the agent's service, or the agent's files. The
    name it returns is the agent's own record of it, which can differ from the
    one asked for. A plugin without the hook cannot be renamed from here, and
    one that lists the `rename` capability must have it.
    """
    hook = cast("RenameHook | None", getattr(await load(agent), "rename", None))
    if hook is None:
        raise RcError("unsupported", f"{agent} sessions cannot be renamed from here")
    return await hook(spec)
