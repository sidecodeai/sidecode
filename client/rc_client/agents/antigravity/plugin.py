"""What this device offers for Antigravity, and how one of its sessions is built (A64)."""

from __future__ import annotations

import asyncio

from ...errors import RcError
from ...models import AgentAccount, AgentInfo, Command, Session
from ..base import SessionRunner
from ..registry import DetectContext, RunnerSpec
from . import catalog, errors, opening, runtime
from .commands import recall
from .options import EFFORTS, offered_modes
from .runner import AntigravityRunner
from .service import AntigravityService

AGENT = "antigravity"

# No `takeover`, `rename` or `close_terminal`: `agy` offers a client no way into a terminal's
# conversation and nothing that renames one (A64).
CAPABILITIES = [
    "worktree",
    "interrupt",
    "queue",
    "steer",
    "attachments",
    "effort",
    "history",
    "commands",
]
# What a message or a command to a conversation an `agy` may have open is refused with (4.4).
TERMINAL_CONFLICT = errors.HELD

# The one service of this device's: the list process, the presence scan and the quota readings.
SERVICE = AntigravityService()


def service() -> AntigravityService:
    """The service the daemon starts once Antigravity has started, and stops with the device."""
    return SERVICE


async def detect(context: DetectContext) -> AgentInfo:
    """What `agy` offers, with no process of its started and nothing of its files read.

    The models and the defaults are what the newest session the device opened was offered. Only a
    `device.agents` reply carries the account, from `agy`'s own usage query (4.2).
    """
    path = runtime.resolve_binary()
    version: str | None = None
    accounts: list[AgentAccount] | None = None
    if path and context.limits:
        version, accounts = await asyncio.gather(runtime.probe_version(path), _accounts(path))
    elif path:
        version = await runtime.probe_version(path)
    offered = catalog.load()
    return AgentInfo(
        agent=AGENT,
        available=bool(path),
        version=version,
        path=path,
        models=list(offered.models),
        default_model=offered.default_model,
        permission_modes=offered_modes(offered.modes),
        default_permission_mode=offered.default_mode,
        efforts=list(EFFORTS),
        default_effort=offered.default_effort,
        capabilities=list(CAPABILITIES),
        attach=None,
        accounts=accounts,
    )


async def _accounts(path: str) -> list[AgentAccount]:
    """The Google account and its windows; none when `agy` last said nobody is signed in."""
    if SERVICE.signed_out:
        return []
    return [await SERVICE.usage.account(path)]


async def commands(session: Session) -> list[Command]:
    """What a session with no live process offers: the list `agy` last advertised here (A27)."""
    return recall()


async def build_runner(spec: RunnerSpec) -> SessionRunner:
    """A runner for a new session, or for one the device opens again (`opening`).

    Everything that refuses the open is decided here, before anything starts: an `agy` too old, a
    conversation an `agy` the device did not start may have open, one `agy` no longer lists.
    """
    path = spec.info.path
    if not path:
        raise RcError("agent_unavailable", errors.NOT_INSTALLED)
    opening.refuse_old(spec.info.version)
    plan = await opening.plan(SERVICE, spec.entry, path) if spec.resume else opening.NEW
    return AntigravityRunner(
        spec.entry,
        binary=path,
        opening=plan,
        service=SERVICE,
        on_turn_end=spec.on_turn_end,
        on_session_id=spec.on_session_id,
    )
