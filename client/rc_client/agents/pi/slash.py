"""pi's slash commands: what a session offers, and what the device adds (A27).

pi calls three things commands — the prompt templates and the skills it finds
on disk, and the commands an extension registers — and lists all three through
`get_commands` in its RPC mode and `pi.getCommands()` inside an extension. None
of its own TUI commands are in that list: pi's documentation says they are
handled only in interactive mode and would not run if sent as a prompt, and a
probe confirmed it. The one built-in worth having from an app is therefore
`compact`, which pi exposes as a command of its own rather than as text, and
the device adds it to every list here.

A session with no live process is answered from disk instead, and it is
answered the way that session would actually be started: `pi --mode rpc
--no-approve` ignores the working directory's own `.pi`, so only the global
prompt and skill directories count. Verified against the real pi 0.85.1, which
listed a project template under `--approve` and not under `--no-approve`.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from ...models import Command
from . import runtime

COMPACT = "compact"
# Compaction summarises the whole session with a model call of its own, so it
# takes far longer than the ordinary command round trip.
COMPACT_TIMEOUT = 600.0

BUILT_IN = "Built-in"
# pi's word for where a command came from, in the protocol's vocabulary (4.11).
GROUPS = {"extension": "Extensions", "prompt": "Prompts", "skill": "Skills"}

# `Command.name` in `protocol/schema/objects.json`. A file whose name does not
# match it cannot be offered at all, because an app could not send it back.
NAME = re.compile(r"^[a-z0-9][a-z0-9_:.-]*$")

# One row in a list on a phone. Some skills write a paragraph of trigger words.
MAX_DESCRIPTION = 200
# pi's own rule for a prompt template with no `description` of its own.
MAX_FIRST_LINE = 60


def compact() -> Command:
    """pi's own words, from `BUILTIN_SLASH_COMMANDS` in its bundle."""
    return Command(
        COMPACT,
        "Manually compact the session context",
        argument="instructions",
        group=BUILT_IN,
    )


def typed(name: str, argument: str | None) -> str:
    """What the person typed, which is what their bubble shows."""
    return f"/{name} {argument}" if argument else f"/{name}"


def from_agent(listed: Any) -> list[Command]:
    """pi's own list of commands for a live session, plus `compact`.

    pi carries no argument hint there — only the name, the description and
    where the entry was loaded from — so a prompt template's hint is read back
    out of the file the entry names, which is a file on this machine.
    """
    commands = [compact()]
    for entry in listed if isinstance(listed, list) else []:
        command = _from_entry(entry)
        if command is not None:
            commands.append(command)
    return _unique(commands)


def offline() -> list[Command]:
    """What the device can say about a session it has not started."""
    return _unique([compact(), *_prompts(), *_skills()])


# --------------------------------------------------------------- pi's own list


def _from_entry(entry: Any) -> Command | None:
    """One `SlashCommandInfo`: `{name, description?, source, sourceInfo}`."""
    if not isinstance(entry, dict):
        return None
    name = str(entry.get("name") or "")
    if name == COMPACT or not NAME.match(name):
        return None
    source = str(entry.get("source") or "")
    info = entry.get("sourceInfo")
    path = str(info.get("path") or "") if isinstance(info, dict) else ""
    return Command(
        name,
        _line(str(entry.get("description") or "")),
        argument=_hint(path) if source == "prompt" else None,
        group=GROUPS.get(source),
    )


def _hint(path: str) -> str | None:
    """A prompt template's `argument-hint`, which pi's list leaves out.

    A command with no file of its own — pi's bundled llama.cpp extension, for
    one — reports a `<inline:…>` placeholder instead of a path.
    """
    if not path or path.startswith("<"):
        return None
    return _frontmatter(Path(path))[0].get("argument-hint") or None


# ------------------------------------------------------------------- on disk


def _prompts() -> list[Command]:
    """`~/.pi/agent/prompts/*.md`, non-recursive, as pi loads them."""
    directory = runtime.home() / "agent" / "prompts"
    commands: list[Command] = []
    if not directory.is_dir():
        return commands
    for path in sorted(directory.glob("*.md")):
        if not path.is_file() or not NAME.match(path.stem):
            continue
        front, body = _frontmatter(path)
        commands.append(
            Command(
                path.stem,
                _line(front.get("description") or _first_line(body)),
                argument=front.get("argument-hint") or None,
                group=GROUPS["prompt"],
            )
        )
    return commands


def _skills() -> list[Command]:
    """The two global skill directories pi reads whatever the project trust is.

    A project's own `.pi/skills` needs the trust `--no-approve` withholds, so a
    session this device starts never sees it and this list never claims it.
    `~/.agents/skills` is shared with other harnesses and its root `.md` files
    are not skills there, unlike pi's own directory.
    """
    # `~/.agents/skills` hangs off the same home `~/.pi` does, so it is derived
    # from that one rather than read again: moving pi's home moves both.
    roots = (
        (runtime.home() / "agent" / "skills", True),
        (runtime.home().parent / ".agents" / "skills", False),
    )
    commands: list[Command] = []
    for root, root_files in roots:
        for path in _skill_files(root, root_files):
            command = _skill(path)
            if command is not None:
                commands.append(command)
    return commands


def _skill_files(directory: Path, root_files: bool) -> list[Path]:
    """pi's own walk: a directory holding `SKILL.md` is one skill and is not
    descended into; otherwise its subdirectories are searched, and its own
    `.md` files count at the top level only."""
    if not directory.is_dir():
        return []
    declared = directory / "SKILL.md"
    if declared.is_file():
        return [declared]
    found: list[Path] = []
    try:
        entries = sorted(directory.iterdir())
    except OSError:
        return found
    for entry in entries:
        if entry.name.startswith(".") or entry.name == "node_modules":
            continue
        if entry.is_dir():
            found.extend(_skill_files(entry, False))
        elif root_files and entry.is_file() and entry.suffix == ".md":
            found.append(entry)
    return found


def _skill(path: Path) -> Command | None:
    """A skill is its frontmatter: no description, no skill, as pi has it."""
    front, _body = _frontmatter(path)
    description = front.get("description", "").strip()
    if not description:
        return None
    name = front.get("name", "").strip() or path.parent.name
    if not NAME.match(name):
        return None
    return Command(f"skill:{name}", _line(description), group=GROUPS["skill"])


# ------------------------------------------------------------------ the files


def _frontmatter(path: Path) -> tuple[dict[str, str], str]:
    """The `---` block's flat `key: value` lines, and the body after it.

    pi parses that block as YAML. The three keys read here — `name`,
    `description` and `argument-hint` — are single-line scalars in every file
    pi's own documentation shows, so a whole YAML parser would buy nothing. A
    key written as a block scalar or a list is skipped rather than guessed at.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}, ""
    text = text.lstrip("﻿").replace("\r\n", "\n").replace("\r", "\n")
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end < 0:
        return {}, text
    fields: dict[str, str] = {}
    for line in text[4:end].split("\n"):
        if not line or line[0] in "#-\t ":
            continue
        key, separator, value = line.partition(":")
        if separator:
            fields[key.strip()] = _scalar(value.strip())
    return fields, text[end + 4 :].strip()


def _scalar(value: str) -> str:
    """A quoted YAML scalar without its quotes; anything else unchanged."""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def _first_line(body: str) -> str:
    """pi's fallback description: the first line of the template itself."""
    for line in body.split("\n"):
        if line.strip():
            return f"{line[:MAX_FIRST_LINE]}..." if len(line) > MAX_FIRST_LINE else line
    return ""


def _line(text: str) -> str:
    """One row's worth of description, whatever the file wrote."""
    collapsed = " ".join(str(text).split())
    if len(collapsed) <= MAX_DESCRIPTION:
        return collapsed
    return collapsed[: MAX_DESCRIPTION - 1].rstrip() + "…"


def _unique(commands: list[Command]) -> list[Command]:
    """One row per name, keeping the first, which is the one pi would run."""
    seen: set[str] = set()
    kept: list[Command] = []
    for command in commands:
        if command.name in seen:
            continue
        seen.add(command.name)
        kept.append(command)
    return kept
