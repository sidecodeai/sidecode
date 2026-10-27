"""The slash commands a Grok session offers, and the list the device remembers.

Grok Build pushes its whole command list over ACP as an `available_commands_update`
the moment a session opens — built-in shell commands, skills, plugin commands and
workflows — and runs any of them locally when a prompt's text begins with the name,
answering in `agent_message_chunk` at no cost in model tokens. Everything here maps
that list onto the protocol's `Command` (A27): the exclusions below, one line of
description, the hint an app shows as the argument placeholder, and the group it
sections a long list by.

The last list is kept in the device home so a session with no live process can still
answer `session.commands`, after a restart of the daemon too.
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

log = logger("rc_client.grok.commands")

STORE_NAME = "grok-commands.json"
STORE_VERSION = 1

# `description` is one line beside `/name` in a composer, and a Grok skill's own
# description can run to a paragraph of trigger phrases; `input.hint` is the
# placeholder that follows the name, and a plugin's can be a whole usage line.
MAX_DESCRIPTION = 160
MAX_ARGUMENT = 80

# The protocol's own rule for a command name (§4.11). A skill whose name breaks it
# could never be filtered or sent back unchanged, so it is not offered at all.
NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_:.-]*$")

# Groups an app sections the list by. Grok separates its own shell commands from
# what a person installed, which is the distinction worth showing on a phone.
BUILT_IN = "Built-in"
SKILLS = "Skills"
PLUGINS = "Plugins"
WORKFLOWS = "Workflows"

# Names Grok advertises that an app must never list, each with its reason. Derived
# from the advertisement of a real Grok Build 1.0.30 session recorded in
# `tests/fixtures/grok/available-commands.json`.
EXCLUDED: dict[str, str] = {
    # Permission modes are `session.set`, so listing this would give a phone two
    # controls for one thing — and one of them a single tap away from approving
    # everything (A27: commands are not settings).
    "always-approve": "permission modes are a session setting, not a command",
    # Verified against the real agent: the turn ends in 10 ms having emitted no
    # update at all, because the TUI renders it in its own pager.
    "context": "renders in the terminal's pager and sends nothing over ACP",
    # Configures the Grok Build status line, a terminal surface no app draws.
    "statusline": "configures the terminal status line, which an app never shows",
    # Naming a session is `session.set` (A55), so listing `/rename` and its
    # `/title` alias would give a phone two controls for one thing. Grok Build
    # 1.0.30 did not advertise them; they are excluded ahead of a build that does.
    "rename": "a session's name is a session setting, not a command",
    "title": "an alias of /rename",
}


def advertised(update: Mapping[str, Any]) -> list[Command]:
    """The `availableCommands` of one `available_commands_update`, as `Command`s."""
    entries = update.get("availableCommands")
    if not isinstance(entries, list):
        return []
    listed: list[Command] = []
    seen: set[str] = set()
    for entry in entries:
        command = _command(entry)
        if command is None or command.name in seen:
            continue
        seen.add(command.name)
        listed.append(command)
    return listed


def _command(entry: Any) -> Command | None:
    if not isinstance(entry, Mapping):
        return None
    name = str(entry.get("name") or "").strip()
    if not name or name in EXCLUDED or not NAME_PATTERN.match(name):
        return None
    description = _one_line(str(entry.get("description") or ""), MAX_DESCRIPTION)
    return Command(
        name=name,
        description=description or name,
        argument=_argument(entry.get("input")),
        group=_group(entry.get("_meta")),
    )


def _one_line(text: str, limit: int) -> str:
    """One line of at most `limit` characters, cut on a word boundary where there is one."""
    collapsed = " ".join(text.split())
    if len(collapsed) <= limit:
        return collapsed
    head = collapsed[: limit - 1]
    space = head.rfind(" ")
    if space > limit // 2:
        head = head[:space]
    return head.rstrip(" ,;:.-") + "…"


def _argument(raw: Any) -> str | None:
    if not isinstance(raw, Mapping):
        return None
    hint = _one_line(str(raw.get("hint") or ""), MAX_ARGUMENT)
    return hint or None


def _group(meta: Any) -> str:
    """Where the command comes from, read off the `_meta` Grok attaches to each entry.

    A built-in shell command carries no `_meta` at all; a skill carries `scope`, a
    plugin's command carries `pluginName` on top of it, and a workflow carries
    `workflowSource` instead.
    """
    if not isinstance(meta, Mapping):
        return BUILT_IN
    if meta.get("workflowSource") or meta.get("workflowPath"):
        return WORKFLOWS
    if meta.get("pluginName"):
        return PLUGINS
    if meta.get("scope"):
        return SKILLS
    return BUILT_IN


# --------------------------------------------------------------------- the store


def store_path() -> Path:
    """Beside the rest of the device's state, so it survives a restart."""
    return state_dir() / STORE_NAME


def recall() -> list[Command]:
    """The list Grok last advertised on this device; empty when it never has."""
    try:
        raw = store_path().read_bytes()
    except OSError:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        log.warning("ignoring an unreadable grok command store")
        return []
    if not isinstance(data, dict) or data.get("version") != STORE_VERSION:
        return []
    return _from_dicts(data.get("commands"))


def remember(commands: Sequence[Command]) -> None:
    """Keep the list for a session with no process. Never fails a live session."""
    rows = [command.to_dict() for command in commands]
    if _from_dicts(rows) == recall():
        return
    payload = {"version": STORE_VERSION, "updated_ms": now_ms(), "commands": rows}
    try:
        write_atomic(store_path(), json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    except OSError:
        log.warning("could not write the grok command store")


def _from_dicts(rows: Any) -> list[Command]:
    """Rebuild the stored list, dropping anything this build would not offer now."""
    if not isinstance(rows, list):
        return []
    listed: list[Command] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        name = str(row.get("name") or "")
        description = str(row.get("description") or "")
        if not description or name in EXCLUDED or not NAME_PATTERN.match(name):
            continue
        listed.append(
            Command(
                name=name,
                description=description,
                argument=str(row.get("argument") or "") or None,
                group=str(row.get("group") or "") or None,
            )
        )
    return listed
