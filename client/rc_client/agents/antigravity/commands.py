"""The slash commands an Antigravity session offers, and the list the device remembers (A27).

`agy` advertises its commands over ACP when a session opens — its workflows and skills, a person's
own among them — each a name and a description with no argument hint, and runs one when a prompt's
text begins with it, `/compact` answering with no text at all (A64). Nothing it advertises is a
setting or a terminal control, so the list is offered as it comes. The last list is kept in the
device's state, so a session with no process can still answer `session.commands`.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from ...config import state_dir, write_atomic
from ...logging_setup import logger
from ...models import Command, now_ms

log = logger("rc_client.antigravity.commands")

STORE_NAME = "antigravity-commands.json"
STORE_VERSION = 1
MAX_DESCRIPTION = 160
# The protocol's own rule for a command name (4.11).
NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_:.-]*$")


def advertised(update: Mapping[str, Any]) -> list[Command]:
    """The `availableCommands` of one `available_commands_update`, as `Command`s."""
    entries = update.get("availableCommands")
    listed: list[Command] = []
    seen: set[str] = set()
    for entry in entries if isinstance(entries, list) else []:
        command = _command(entry)
        if command is not None and command.name not in seen:
            seen.add(command.name)
            listed.append(command)
    return listed


def typed(name: str, argument: str | None) -> str:
    """The text of the turn that runs a command, as a person would type it."""
    return f"/{name} {argument}" if argument else f"/{name}"


def _command(entry: Any) -> Command | None:
    if not isinstance(entry, Mapping):
        return None
    name = str(entry.get("name") or "").strip()
    if not NAME_PATTERN.match(name):
        return None
    description = _one_line(str(entry.get("description") or ""))
    return Command(name=name, description=description or name)


def _one_line(text: str) -> str:
    collapsed = " ".join(text.split())
    if len(collapsed) <= MAX_DESCRIPTION:
        return collapsed
    head = collapsed[: MAX_DESCRIPTION - 1]
    space = head.rfind(" ")
    if space > MAX_DESCRIPTION // 2:
        head = head[:space]
    return head.rstrip(" ,;:.-") + "…"


def store_path() -> Path:
    return state_dir() / STORE_NAME


def recall() -> list[Command]:
    """The list `agy` last advertised on this device; empty when it never has."""
    try:
        data = json.loads(store_path().read_bytes())
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(data, dict) or data.get("version") != STORE_VERSION:
        return []
    rows = data.get("commands")
    listed: list[Command] = []
    for row in rows if isinstance(rows, list) else []:
        command = _command(row)
        if command is not None:
            listed.append(command)
    return listed


def remember(commands: Sequence[Command]) -> None:
    """Keep the list for a session with no process. Never fails a live session."""
    if list(commands) == recall():
        return
    payload = {
        "version": STORE_VERSION,
        "updated_ms": now_ms(),
        "commands": [command.to_dict() for command in commands],
    }
    try:
        write_atomic(store_path(), json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    except OSError:
        log.warning("could not keep the Antigravity command list")
