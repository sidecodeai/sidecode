"""What this device offers for the Codex CLI, and how one of its sessions is built."""

from __future__ import annotations

from ... import __version__
from ...errors import RcError
from ...models import AgentInfo, Choice, Command, Session
from ..base import SessionRunner
from ..registry import DetectContext, RunnerSpec
from . import account, runtime
from . import commands as slash
from .adapter import CodexRunner
from .daemon.rpc import handshake_ok
from .daemon.transport import socket_exists
from .models import ModelCatalog, catalog_cache

AGENT = "codex"

PERMISSION_MODES = [
    Choice("untrusted", "Ask for everything"),
    Choice("on-request", "Ask when needed"),
    Choice("never", "Never ask"),
]
CAPABILITIES = [
    "interrupt",
    "queue",
    "steer",
    "history",
    "worktree",
    "attachments",
    "effort",
    "commands",
]


async def daemon_ready() -> bool:
    """A real handshake, not a file-exists check: A11 defines `attach_ready` that way."""
    if not socket_exists():
        return False
    return await handshake_ok(__version__)


async def detect(context: DetectContext) -> AgentInfo:
    path = runtime.resolve_binary()
    version = await runtime.probe_version(path) if path else None
    catalog = await catalog_cache.get(path) if path else None
    known = context.codex_daemon_ready
    ready = await daemon_ready() if known is None else known
    accounts = await account.detect(context.limits, context.codex_rate_limits) if path else None
    return AgentInfo(
        agent=AGENT,
        available=bool(path),
        version=version,
        path=path,
        models=list(catalog.models) if catalog else [],
        default_model=catalog.default_model if catalog else None,
        permission_modes=list(PERMISSION_MODES),
        default_permission_mode="on-request",
        efforts=list(catalog.efforts) if catalog else [],
        default_effort=catalog.default_effort if catalog else None,
        speeds=list(catalog.speeds) if catalog else [],
        capabilities=list(CAPABILITIES),
        attach="daemon",
        attach_ready=ready,
        shared_interrupt=True,
        shared_settings=True,
        shared_attachments=True,
        accounts=accounts,
    )


async def commands(session: Session) -> list[Command]:
    """The table a session offers before anything is running (A27).

    Codex's list is fixed, so a session with no process answers exactly what a
    live one does and an app can draw the menu without waking the agent.
    """
    return slash.listing()


async def build_runner(spec: RunnerSpec) -> SessionRunner:
    daemon = spec.codex_daemon
    if daemon is not None and daemon.ready:
        return daemon.session_for(spec.entry, spec.resume)
    if not spec.info.path:
        raise RcError("agent_unavailable", "codex is not installed on this device")
    session = spec.session
    catalog: ModelCatalog = await catalog_cache.get(spec.info.path)
    return CodexRunner(
        spec.channel,
        binary=spec.info.path,
        cwd=session.cwd,
        catalog=catalog,
        model=session.model,
        permission_mode=session.permission_mode,
        effort=session.effort,
        speed=session.speed,
        thread_id=spec.resume,
        on_turn_end=spec.on_turn_end,
        on_session_id=spec.on_session_id,
    )
