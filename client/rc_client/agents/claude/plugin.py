"""What this device offers for Claude Code, and how one of its sessions is built."""

from __future__ import annotations

from ...channel import shim
from ...models import AgentInfo, Choice, Command, Session
from ..base import SessionRunner
from ..registry import DetectContext, RunnerSpec
from . import account, runtime
from .adapter import ClaudeRunner
from .commands import COMMANDS

AGENT = "claude"

MODELS = [
    Choice("default", "Default"),
    Choice("fable", "Fable"),
    Choice("opus", "Opus"),
    Choice("sonnet", "Sonnet"),
    Choice("haiku", "Haiku"),
]
PERMISSION_MODES = [
    Choice("default", "Ask before edits"),
    Choice("acceptEdits", "Auto-accept edits"),
    Choice("plan", "Plan mode"),
    Choice("bypassPermissions", "Bypass permissions"),
]
EFFORTS = [
    Choice("low", "Low"),
    Choice("medium", "Medium"),
    Choice("high", "High"),
    Choice("xhigh", "Extra high"),
    Choice("max", "Max"),
]
CAPABILITIES = [
    "takeover",
    "interrupt",
    "queue",
    "attachments",
    "effort",
    "history",
    "worktree",
    # A40: `/compact` runs on a remote session as a prompt and on a shared one
    # by being typed into the terminal.
    "commands",
]

# What an app may change on a `shared` session, because the device can type it
# into the terminal (A40). The permission mode has no command to type, so it
# stays what the terminal set and an app draws it as a value (A17).
SHARED_SETTINGS_KEYS = ["model", "effort"]

# `default` means "do not pass a model"; the real id arrives from the SDK init
# message and is reported later as `meta.model`.
DEFAULT_MODEL = "default"


async def detect(context: DetectContext) -> AgentInfo:
    path = runtime.resolve_binary()
    version = await runtime.probe_version(path) if path else None
    # An agent that is not here is not signed in anywhere either, and the wire
    # says "the device did not look" by leaving `accounts` out altogether (A33).
    accounts = await account.detect(context.limits) if path else None
    # Typing into the terminal is the shim's doing, so what it can do depends
    # on the shim being installed and first on PATH. Stopping a turn is the
    # same keystroke route — Escape — so it rides on the same condition (A42).
    attachable = shim.status().ready
    return AgentInfo(
        agent=AGENT,
        available=bool(path),
        version=version,
        path=path,
        models=list(MODELS),
        default_model=DEFAULT_MODEL,
        permission_modes=list(PERMISSION_MODES),
        default_permission_mode="default",
        efforts=list(EFFORTS),
        default_effort=None,
        capabilities=list(CAPABILITIES),
        attach="channel",
        attach_ready=attachable,
        shared_interrupt=attachable,
        shared_settings=attachable,
        shared_settings_keys=list(SHARED_SETTINGS_KEYS) if attachable else None,
        shared_attachments=False,
        accounts=accounts,
    )


async def commands(session: Session) -> list[Command]:
    """What a Claude session offers with no process running (A27, A40).

    The list does not depend on the session, and typing `/compact` into an
    attached terminal needs no process of the device's either.
    """
    return list(COMMANDS)


async def build_runner(spec: RunnerSpec) -> SessionRunner:
    session = spec.session
    return ClaudeRunner(
        spec.channel,
        binary=spec.info.path,
        cwd=session.cwd,
        model=session.model,
        permission_mode=session.permission_mode,
        effort=session.effort,
        resume=spec.resume,
        session_id=session.session_id,
        on_turn_end=spec.on_turn_end,
        on_session_id=spec.on_session_id,
    )
