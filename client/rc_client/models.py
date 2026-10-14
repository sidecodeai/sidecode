"""Wire objects from PROTOCOL §3, as dataclasses with `to_dict` serialisers."""

from __future__ import annotations

import time
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Literal

SessionState = Literal[
    "starting", "idle", "running", "needs_approval", "needs_input", "error", "stopped", "readonly"
]
Control = Literal["remote", "terminal", "shared", "none"]
# How a terminal session of this agent can be attached (amendments A10, A26, A28).
Attach = Literal["channel", "daemon", "extension", "leader"]
Origin = Literal["remote", "terminal"]


class Unset(Enum):
    """A field a request never mentioned, which `None` cannot say for a nullable one.

    `Session.speed` is null at the agent's standard speed (amendment A21), so a
    request that clears the tier and a request that says nothing about it both
    arrive as `None` unless they are told apart here.
    """

    TOKEN = "unset"


UNSET = Unset.TOKEN

# A speed tier id, `None` for the agent's standard speed, `UNSET` when unmentioned.
SpeedSetting = str | None | Unset

# How an agent signs in with a vendor: its own subscription, or a key (A33).
AccountMethod = Literal["account", "api_key"]
# The plan word a vendor records: a word, `None` when the vendor records none
# for an account that could have one, `UNSET` when the agent keeps no plan at
# all — the key is then absent, which is what "the device cannot say" means.
PlanSetting = str | None | Unset


# The longest title the apps are given for a session (PROTOCOL.md section 8).
MAX_TITLE = 60


def now_ms() -> int:
    return int(time.time() * 1000)


@dataclass(slots=True)
class Choice:
    """An `{id, label}` pair used for models, permission modes and efforts."""

    id: str
    label: str

    def to_dict(self) -> dict[str, str]:
        return {"id": self.id, "label": self.label}


@dataclass(slots=True)
class Command:
    """A slash command a session offers (amendment A27).

    `name` is what the user types after the slash. `argument` is a placeholder
    for what may follow it, absent when the command takes nothing; `group` says
    where it comes from, absent when the agent draws no distinction.
    """

    name: str
    description: str
    argument: str | None = None
    group: str | None = None

    def to_dict(self) -> dict[str, str]:
        result = {"name": self.name, "description": self.description}
        if self.argument:
            result["argument"] = self.argument
        if self.group:
            result["group"] = self.group
        return result


@dataclass(slots=True)
class AgentLimit:
    """One rate-limit window of a vendor account, as the device read it (A33)."""

    window_minutes: int
    used_percent: float
    resets_at: int | None = None
    # What the window is confined to when it is not everything, in the vendor's
    # words: the model a weekly limit applies to.
    scope: str | None = None

    def __post_init__(self) -> None:
        # The wire says 0-100, so a vendor that reports more does not put a
        # meter past its end; a whole number stays whole.
        percent = min(100.0, max(0.0, float(self.used_percent)))
        self.used_percent = int(percent) if percent.is_integer() else percent

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "window_minutes": self.window_minutes,
            "used_percent": self.used_percent,
        }
        if self.scope:
            result["scope"] = self.scope
        if self.resets_at is not None:
            result["resets_at"] = self.resets_at
        return result


@dataclass(slots=True)
class AgentAccount:
    """One credential an agent holds on this device (A33).

    `limits`, `limits_error` and `limits_checked_at` belong to a `device.agents`
    reply and nowhere else: `hello` and `agents.updated` carry accounts as
    `without_limits` leaves them, so re-detection every quarter hour publishes
    nothing new.
    """

    provider: str
    method: AccountMethod
    plan: PlanSetting = UNSET
    tier: str | None = None
    email: str | None = None
    endpoint: str | None = None
    limits: list[AgentLimit] | None = None
    limits_error: str | None = None
    limits_checked_at: int | None = None

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"provider": self.provider, "method": self.method}
        if not isinstance(self.plan, Unset):
            result["plan"] = self.plan
        for key, value in (("tier", self.tier), ("email", self.email), ("endpoint", self.endpoint)):
            if value:
                result[key] = value
        if self.limits is not None:
            result["limits"] = [limit.to_dict() for limit in self.limits]
        if self.limits_error:
            result["limits_error"] = self.limits_error
        if self.limits_checked_at is not None:
            result["limits_checked_at"] = self.limits_checked_at
        return result

    def without_limits(self) -> AgentAccount:
        return replace(self, limits=None, limits_error=None, limits_checked_at=None)


@dataclass(slots=True)
class AgentInfo:
    agent: str
    available: bool
    version: str | None = None
    path: str | None = None
    models: list[Choice] = field(default_factory=list)
    default_model: str | None = None
    permission_modes: list[Choice] = field(default_factory=list)
    default_permission_mode: str | None = None
    efforts: list[Choice] = field(default_factory=list)
    default_effort: str | None = None
    # Tiers faster than the agent's standard speed; empty when it has none (A21).
    speeds: list[Choice] = field(default_factory=list)
    capabilities: list[str] = field(default_factory=list)
    attach: Attach | None = None
    attach_ready: bool = False
    shared_interrupt: bool = False
    shared_settings: bool = False
    # With `shared_settings` true, the settings an app may change on a `shared`
    # session; the rest stay what the terminal chose (A17). `None` is "all
    # four", which is what every agent but Claude reports (A40).
    shared_settings_keys: list[str] | None = None
    shared_attachments: bool = False
    # How the agent is signed in here; `None` is "the device did not look",
    # which is what an agent that is not installed reports (A33).
    accounts: list[AgentAccount] | None = None

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "agent": self.agent,
            "available": self.available,
            "version": self.version,
            "path": self.path,
            "models": [choice.to_dict() for choice in self.models],
            "default_model": self.default_model,
            "permission_modes": [choice.to_dict() for choice in self.permission_modes],
            "default_permission_mode": self.default_permission_mode,
            "efforts": [choice.to_dict() for choice in self.efforts],
            "default_effort": self.default_effort,
            "speeds": [choice.to_dict() for choice in self.speeds],
            "capabilities": list(self.capabilities),
            "attach": self.attach,
            "attach_ready": self.attach_ready,
            "shared_interrupt": self.shared_interrupt,
            "shared_settings": self.shared_settings,
            "shared_attachments": self.shared_attachments,
        }
        if self.shared_settings_keys is not None:
            result["shared_settings_keys"] = list(self.shared_settings_keys)
        if self.accounts is not None:
            result["accounts"] = [account.to_dict() for account in self.accounts]
        return result

    def without_limits(self) -> AgentInfo:
        """The same agent as `hello` and `agents.updated` report it (A33)."""
        if self.accounts is None:
            return self
        return replace(self, accounts=[account.without_limits() for account in self.accounts])


@dataclass(slots=True)
class SessionResume:
    """The one resume a session can have pending after a usage limit (A35, 7.2).

    `estimated` says the vendor named no reset time and `at` was computed from
    the window's length; `attempts` counts the resumes that ran into the limit
    again, and the device drops the resume after the third.
    """

    at: int
    estimated: bool = False
    attempts: int = 0
    window_minutes: int | None = None

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "at": self.at,
            "estimated": self.estimated,
            "attempts": self.attempts,
        }
        if self.window_minutes is not None:
            result["window_minutes"] = self.window_minutes
        return result

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SessionResume:
        return cls(
            at=int(data["at"]),
            estimated=bool(data.get("estimated")),
            attempts=int(data.get("attempts") or 0),
            window_minutes=data.get("window_minutes"),
        )


@dataclass(slots=True)
class Session:
    session_id: str
    device_id: str
    agent: str
    cwd: str
    title: str = ""
    git: dict[str, Any] | None = None
    state: SessionState = "starting"
    state_detail: str | None = None
    origin: Origin = "remote"
    control: Control = "remote"
    model: str | None = None
    permission_mode: str | None = None
    effort: str | None = None
    # The tier from `AgentInfo.speeds` in force; null is the standard speed (A21).
    speed: str | None = None
    created_at: int = field(default_factory=now_ms)
    updated_at: int = field(default_factory=now_ms)
    last_seq: int = 0
    archived: bool = False
    turn: dict[str, Any] | None = None
    todos: dict[str, int] | None = None
    usage: dict[str, Any] | None = None
    queued: int = 0
    # The resume the device has scheduled after a usage limit, null when there
    # is none (amendment A35). Always published, so an app that redraws from a
    # summary is never left holding one the device has dropped.
    resume: SessionResume | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "device_id": self.device_id,
            "agent": self.agent,
            "title": self.title,
            "cwd": self.cwd,
            "git": self.git,
            "state": self.state,
            "state_detail": self.state_detail,
            "origin": self.origin,
            "control": self.control,
            "model": self.model,
            "permission_mode": self.permission_mode,
            "effort": self.effort,
            "speed": self.speed,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "last_seq": self.last_seq,
            "archived": self.archived,
            "turn": self.turn,
            "todos": self.todos,
            "usage": self.usage,
            "queued": self.queued,
            "resume": self.resume.to_dict() if self.resume else None,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Session:
        resume = data.get("resume")
        return cls(
            session_id=str(data["session_id"]),
            device_id=str(data.get("device_id") or ""),
            agent=str(data["agent"]),
            cwd=str(data.get("cwd") or ""),
            title=str(data.get("title") or ""),
            git=data.get("git"),
            state=data.get("state", "idle"),
            state_detail=data.get("state_detail"),
            origin=data.get("origin", "remote"),
            control=data.get("control", "none"),
            model=data.get("model"),
            permission_mode=data.get("permission_mode"),
            effort=data.get("effort"),
            speed=data.get("speed"),
            created_at=int(data.get("created_at") or now_ms()),
            updated_at=int(data.get("updated_at") or now_ms()),
            last_seq=int(data.get("last_seq") or 0),
            archived=bool(data.get("archived")),
            turn=data.get("turn"),
            todos=data.get("todos"),
            usage=data.get("usage"),
            queued=int(data.get("queued") or 0),
            resume=SessionResume.from_dict(resume) if isinstance(resume, dict) else None,
        )


def title_from_text(text: str) -> str:
    """The first line of some text, capped at `MAX_TITLE` (PROTOCOL §8 rule)."""
    line = text.strip().splitlines()[0].strip() if text.strip() else ""
    if len(line) > MAX_TITLE:
        return line[: MAX_TITLE - 1].rstrip() + "…"
    return line


def title_from_message(text: str) -> str:
    """A title taken from something somebody said, or "" for a slash command.

    A command names what the person did to the CLI, not what the conversation
    is about, and Claude Code itself never titles a session from one. Typed
    commands became messages with amendment A32, so without this rule a session
    whose first message was `/compact` would be called that for good. A title
    the person typed themselves is taken as given, slash and all, and goes
    through `title_from_text`.
    """
    return "" if text.strip().startswith("/") else title_from_text(text)
