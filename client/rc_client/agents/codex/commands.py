"""The slash commands a Codex session offers, and how each one runs (A27).

Codex's app-server interprets nothing that begins with a slash: a `turn/start`
whose text is `/status` produces a model turn answering the literal string. So
the list is a fixed table rather than something the agent is asked for, and
every entry is a named method call. One implementation serves both connections,
the private app-server of `adapter.py` and the shared daemon of
`daemon/session.py`, because they differ only in which client carries a request.

Settings, lifecycle and terminal ergonomics are never listed: `/model`,
`/permissions` and `/fast` are `session.set`, `/new`, `/archive` and `/delete`
are frames of their own, and `/vim`, `/theme` and `/copy` change a terminal the
apps cannot see.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from ...errors import RcError
from ...models import Command, now_ms
from ...sessions.channel import SessionChannel
from . import reports
from .init_prompt import INIT_PROMPT
from .reports import ThreadFacts

# Codex's own words from the TUI's `/` popup, trimmed only where the terminal
# promises something an app does not get: `/usage` cannot spend a limit reset
# from here, `/hooks` and `/skills` are read-only, and `/mcp verbose` is a
# terminal flag. One source, so no entry carries a group.
COMMANDS: tuple[Command, ...] = (
    Command("compact", "summarize the current conversation now"),
    Command("review", "review my current changes and find issues", argument="instructions"),
    Command("init", "create an AGENTS.md file with instructions for Codex"),
    Command("diff", "show git diff (including untracked files)"),
    Command("status", "show current session configuration and token usage"),
    Command("usage", "view account usage"),
    Command("skills", "list the skills Codex can use here"),
    Command("hooks", "view lifecycle hooks"),
    Command("mcp", "list configured MCP tools"),
)

# Longer than a turn's own requests: `skills/list` walks every skill directory.
REPORT_TIMEOUT = 30.0
DIFF_TIMEOUT = 30.0


class CommandHost(Protocol):
    """What a Codex session has to offer for its commands to run."""

    channel: SessionChannel

    @property
    def thread_id(self) -> str | None: ...

    def facts(self) -> ThreadFacts: ...

    async def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """One request on whichever app-server connection this session uses."""
        ...

    async def run_prompt(self, prompt: str, shown: str, block_id: str) -> None:
        """Start a turn with `prompt` while the bubble reads `shown`.

        Codex echoes the prompt it was given as a `userMessage` item, so the
        runner records the echo to keep that item from becoming a second bubble
        saying what the user did not type.
        """
        ...


def listing() -> list[Command]:
    """The table, which every Codex session offers whatever it is attached to."""
    return list(COMMANDS)


def typed(name: str, argument: str | None) -> str:
    """What the user typed, rebuilt for the bubble."""
    return f"/{name} {argument}" if argument else f"/{name}"


async def run(host: CommandHost, name: str, argument: str | None, block_id: str) -> None:
    """Run one command, echoing it first and reporting the outcome as events."""
    handler = _HANDLERS.get(name)
    if handler is None:
        raise RcError("not_found", f"/{name} is not a command this session offers")
    thread_id = host.thread_id
    if thread_id is None:
        raise RcError("agent_unavailable", "the Codex session is not connected")
    await handler(host, thread_id, argument, block_id)


# --------------------------------------------------------------- publication


async def _echo(host: CommandHost, text: str, block_id: str) -> None:
    """The `user_message` for the command, under the app's own request id (A12)."""
    fields: dict[str, Any] = {"block_id": block_id, "text": text, "source": "remote"}
    if host.channel.session.control == "shared":
        fields["delivery"] = "delivered"
    await host.channel.emit("user_message", **fields)


async def _report(
    host: CommandHost,
    name: str,
    block_id: str,
    produce: Callable[[], Awaitable[tuple[str, str | None]]],
) -> None:
    """Run an information command as the one block a terminal would have printed."""
    await _echo(host, f"/{name}", block_id)
    started = now_ms()
    base: dict[str, Any] = {
        "block_id": f"command:{block_id}",
        "tool": f"/{name}",
        "tool_kind": "other",
        "title": f"/{name}",
        "started_at": started,
    }
    await host.channel.emit("tool_call", status="running", **base)
    text, summary = await produce()
    ended = now_ms()
    fields = dict(
        base,
        status="succeeded",
        output=text,
        ended_at=ended,
        duration_ms=max(0, ended - started),
    )
    if summary:
        fields["summary"] = summary
    await host.channel.emit("tool_call", **fields)


# ------------------------------------------------------------------ handlers


async def _compact(host: CommandHost, thread_id: str, argument: str | None, block: str) -> None:
    """Codex compacts inside a turn of its own, which reports itself."""
    await _echo(host, "/compact", block)
    await host.call("thread/compact/start", {"threadId": thread_id})


async def _review(host: CommandHost, thread_id: str, argument: str | None, block: str) -> None:
    """No argument reviews the working tree; an argument is the brief for it."""
    await _echo(host, typed("review", argument), block)
    target = (
        {"type": "custom", "instructions": argument} if argument else {"type": "uncommittedChanges"}
    )
    await host.call("review/start", {"threadId": thread_id, "target": target, "delivery": "inline"})


async def _init(host: CommandHost, thread_id: str, argument: str | None, block: str) -> None:
    """A canned prompt, not a method: the bubble says `/init`, the model reads the prompt."""
    await host.run_prompt(INIT_PROMPT, "/init", block)


async def _status(host: CommandHost, thread_id: str, argument: str | None, block: str) -> None:
    facts = host.facts()

    async def produce() -> tuple[str, str | None]:
        name = await _fill_from_thread(host, thread_id, facts)
        if facts.sandbox is None:
            # A thread the daemon would not resume never told this device what
            # sandbox it runs under; the configuration is the next best thing.
            result = await host.call("config/read", {"cwd": facts.cwd})
            config = result.get("config")
            if isinstance(config, dict) and config.get("sandbox_mode"):
                facts.sandbox = {"type": _camel(str(config["sandbox_mode"]))}
        return reports.status_text(facts, name), facts.model

    await _report(host, "status", block, produce)


async def _usage(host: CommandHost, thread_id: str, argument: str | None, block: str) -> None:
    async def produce() -> tuple[str, str | None]:
        usage = await host.call("account/usage/read", {})
        limits = await host.call("account/rateLimits/read", {})
        text = reports.usage_text(usage, limits)
        return text, text.splitlines()[0] if text else None

    await _report(host, "usage", block, produce)


async def _skills(host: CommandHost, thread_id: str, argument: str | None, block: str) -> None:
    async def produce() -> tuple[str, str | None]:
        result = await host.call("skills/list", {"cwds": [host.facts().cwd]})
        text = reports.skills_text(result.get("data"))
        return text, text.splitlines()[0]

    await _report(host, "skills", block, produce)


async def _hooks(host: CommandHost, thread_id: str, argument: str | None, block: str) -> None:
    async def produce() -> tuple[str, str | None]:
        result = await host.call("hooks/list", {"cwds": [host.facts().cwd]})
        text = reports.hooks_text(result.get("data"))
        return text, text.splitlines()[0]

    await _report(host, "hooks", block, produce)


async def _mcp(host: CommandHost, thread_id: str, argument: str | None, block: str) -> None:
    async def produce() -> tuple[str, str | None]:
        result = await host.call(
            "mcpServerStatus/list", {"threadId": thread_id, "detail": "toolsAndAuthOnly"}
        )
        text = reports.mcp_text(result.get("data"))
        return text, text.splitlines()[0]

    await _report(host, "mcp", block, produce)


async def _diff(host: CommandHost, thread_id: str, argument: str | None, block: str) -> None:
    cwd = host.facts().cwd

    async def produce() -> tuple[str, str | None]:
        stat = await _git(host, cwd, ["diff", "--stat"])
        if stat is None:
            return "not inside a git repository", "not a git repository"
        patch = await _git(host, cwd, ["diff"]) or ""
        listed = await _git(host, cwd, ["ls-files", "--others", "--exclude-standard"]) or ""
        untracked = [line for line in listed.splitlines() if line.strip()]
        return reports.diff_text(stat, patch, untracked), reports.diff_summary(stat, untracked)

    await _report(host, "diff", block, produce)


# -------------------------------------------------------------------- shared


def _camel(value: str) -> str:
    """`workspace-write` as Codex spells it in a thread's own answer."""
    head, *rest = value.split("-")
    return head + "".join(part[:1].upper() + part[1:] for part in rest)


async def _fill_from_thread(host: CommandHost, thread_id: str, facts: ThreadFacts) -> str | None:
    """The thread's own name, and whatever settings the session never learned.

    A thread with no rollout yet is one the daemon refuses to resume, so its
    settings never reached the session; `thread/read` answers for it anyway.
    """
    try:
        result = await host.call("thread/read", {"threadId": thread_id})
    except RcError:
        return None
    thread = result.get("thread")
    if not isinstance(thread, dict):
        return None
    if facts.model is None and isinstance(thread.get("model"), str):
        facts.model = str(thread["model"]) or None
    if facts.effort is None and isinstance(thread.get("reasoningEffort"), str):
        facts.effort = str(thread["reasoningEffort"]) or None
    name = thread.get("name")
    return str(name) if isinstance(name, str) and name else None


async def _git(host: CommandHost, cwd: str, argv: list[str]) -> str | None:
    """Run one read-only git command beside the thread, off the thread's turn.

    `command/exec` answers with the output rather than appending an item to the
    thread, which is what keeps `/diff` from reading as something the agent did.
    `None` says git refused, which for the first call means no repository.
    """
    result = await host.call("command/exec", {"command": ["git", *argv], "cwd": cwd})
    if int(result.get("exitCode") or 0) != 0:
        return None
    return str(result.get("stdout") or "")


Handler = Callable[[CommandHost, str, str | None, str], Awaitable[None]]

_HANDLERS: dict[str, Handler] = {
    "compact": _compact,
    "review": _review,
    "init": _init,
    "diff": _diff,
    "status": _status,
    "usage": _usage,
    "skills": _skills,
    "hooks": _hooks,
    "mcp": _mcp,
}
