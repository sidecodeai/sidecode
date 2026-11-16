"""What this device offers for Grok Build, and how one of its sessions is built."""

from __future__ import annotations

from ...errors import RcError
from ...models import AgentInfo, Choice, Command, Session
from ..base import SessionRunner
from ..registry import CloseSpec, DetectContext, RenameSpec, RunnerSpec
from . import account, ending, leader, naming, runtime
from . import catalog as catalogue
from .adapter import GrokRunner
from .commands import recall

AGENT = "grok"

PERMISSION_MODES = [
    Choice("default", "Ask when needed"),
    Choice("acceptEdits", "Auto-accept edits"),
    Choice("auto", "Auto mode"),
    Choice("dontAsk", "Deny unless allowed"),
    Choice("plan", "Plan mode"),
    Choice("bypassPermissions", "Bypass permissions"),
]
# `attachments` although this build answers `promptCapabilities.image: false`:
# the device writes each file to disk and the prompt names it, and Grok opens it
# with `read_file` (A49). No `steer`: a prompt sent mid-turn queues.
CAPABILITIES = [
    "worktree",
    "interrupt",
    "queue",
    "attachments",
    "effort",
    "history",
    "commands",
]
# What only the leader makes possible: renaming a session in Grok (A55), and closing
# one a terminal holds, `session/cancel` then SIGTERM to the TUI Grok's registry
# names and `session/close` (A61).
LEADER_CAPABILITIES = ["rename", "close_terminal"]


async def detect(context: DetectContext) -> AgentInfo:
    path = runtime.resolve_binary()
    version = await runtime.probe_version(path) if path else None
    models = catalogue.load()
    accounts = await account.detect(context.limits, context.grok_billing) if path else None
    leader_ready = leader.config_ready()
    return AgentInfo(
        agent=AGENT,
        available=bool(path),
        version=version,
        path=path,
        models=list(models.models),
        default_model=models.default_model,
        permission_modes=list(PERMISSION_MODES),
        default_permission_mode="default",
        efforts=list(models.efforts),
        default_effort=models.default_effort,
        # A55, A61: renaming a session and closing one a terminal holds both go
        # through the leader, so both are listed only while it is ready.
        capabilities=[*CAPABILITIES, *LEADER_CAPABILITIES] if leader_ready else list(CAPABILITIES),
        # Grok Build attaches through its leader: one backend per machine that
        # every `grok` joins when `[cli] use_leader` is on (A28). There is
        # nothing to hand-shake with beforehand, because whichever client comes
        # first starts it, so readiness is the person's own configuration.
        attach="leader",
        attach_ready=leader_ready,
        # `session/cancel` and `session/set_config_option` from any client act
        # on the session every client is in, and a prompt names its files on
        # the leader as it does anywhere else (A49).
        shared_interrupt=True,
        shared_settings=True,
        shared_attachments=True,
        accounts=accounts,
    )


async def commands(session: Session) -> list[Command]:
    """What a session with no live process offers (A27).

    Grok only advertises to a session it has open, so the answer is the list it
    last advertised on this device; a resumed session replaces it with its own.
    """
    return recall()


async def build_runner(spec: RunnerSpec) -> SessionRunner:
    if not spec.info.path:
        raise RcError("agent_unavailable", "grok is not installed on this device")
    service = spec.grok_leader
    if service is not None and await service.ensure(spec.info.path):
        # The same leader every `grok` on this machine joins, so a session
        # started here can be resumed in a terminal and joined live (A28).
        return service.runner_for(spec.entry, spec.resume)
    session = spec.session
    return GrokRunner(
        spec.channel,
        binary=spec.info.path,
        cwd=session.cwd,
        catalog=catalogue.load(),
        model=session.model,
        permission_mode=session.permission_mode,
        effort=session.effort,
        resume=spec.resume,
        on_turn_end=spec.on_turn_end,
        on_session_id=spec.on_session_id,
    )


async def rename(spec: RenameSpec) -> str:
    """A55: `_x.ai/session/rename` through the leader, whatever holds the session."""
    service = spec.grok_leader
    if service is None:
        raise RcError("unsupported", naming.NOT_JOINED)
    await service.rename(spec.session.session_id, spec.session.cwd, spec.name)
    return spec.name


async def close_terminal(spec: CloseSpec) -> bool:
    """A61: end the TUI a terminal runs the session in (`ending.py`)."""
    return await ending.close_terminal(spec)
