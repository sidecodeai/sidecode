"""What this device offers for pi, and how one of its sessions is built."""

from __future__ import annotations

from ...errors import RcError
from ...models import AgentInfo, Command, Session
from ..base import SessionRunner
from ..registry import DetectContext, RunnerSpec
from . import account, install, runtime, slash
from . import catalog as catalogue
from .adapter import PiRunner

AGENT = "pi"

# `attachments` is images and nothing else: pi's `prompt` and its extension both
# take base64 image content, and any other attachment is refused (A26).
# No `takeover`: a pi the device is attached to is already shared, and one it is
# not attached to has no IPC surface to take anything over from.
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


async def detect(context: DetectContext) -> AgentInfo:
    path = runtime.resolve_binary()
    version = await runtime.probe_version(path) if path else None
    catalog = await catalogue.load(path)
    accounts = await account.detect(context.limits) if path else None
    return AgentInfo(
        agent=AGENT,
        available=bool(path),
        version=version,
        path=path,
        models=list(catalog.models),
        default_model=catalog.default_model,
        # pi has no permission system of its own; these three are the device's,
        # enforced by the extension it installs (A26).
        permission_modes=list(catalogue.PERMISSION_MODES),
        default_permission_mode=catalogue.DEFAULT_PERMISSION_MODE,
        efforts=list(catalog.efforts),
        default_effort=catalog.default_effort,
        capabilities=list(CAPABILITIES),
        # The extension runs inside the pi process, so an attached session can
        # be interrupted, re-modelled and handed images like any other.
        attach="extension",
        attach_ready=install.ready(),
        shared_interrupt=True,
        shared_settings=True,
        shared_attachments=True,
        accounts=accounts,
    )


async def commands(session: Session) -> list[Command]:
    """What a pi session offers with no process running (A27).

    The working directory is not consulted: the session would be resumed with
    `--no-approve`, which makes pi ignore that directory's own prompts and
    skills, so only the global ones can be promised.
    """
    return slash.offline()


async def build_runner(spec: RunnerSpec) -> SessionRunner:
    if not spec.info.path:
        raise RcError("agent_unavailable", "pi is not installed on this device")
    session = spec.session
    return PiRunner(
        spec.channel,
        binary=spec.info.path,
        cwd=session.cwd,
        # `--session-id` creates the session when it is new and reopens it when
        # it is not, so resuming and starting take the same path.
        session_id=spec.resume or session.session_id,
        model=session.model,
        effort=session.effort,
        permission_mode=session.permission_mode or catalogue.DEFAULT_PERMISSION_MODE,
        on_turn_end=spec.on_turn_end,
        on_session_id=spec.on_session_id,
    )
