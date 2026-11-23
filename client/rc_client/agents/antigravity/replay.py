"""What the device has published of an Antigravity conversation, so a replay adds only the rest.

`session/load` replays a whole conversation before it answers: the person's words, `agy`'s text and
each tool once, in its final state and under the tool-call id it had live, with no times and no ids
for the words (A64). So the device keeps, for each conversation, the tool ids it has published and,
between two tools, how many of the person's messages and of `agy`'s texts it has published. A
replayed tool it knows is skipped, and so is a text whose place between tools it has already filled;
everything else is published, timed when it was read. The person's message is one row however many
content blocks it had, and `agy`'s text one row per text it replays: a text it went on with by
itself
after its prompt had answered is a row of its own, as the device published it.

The record also keeps when the device itself last saw `agy` write the conversation, which is what
decides between `session/resume` and `session/load` the next time it is opened.
"""

from __future__ import annotations

import json
import mimetypes
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import unquote, urlsplit

from ...registry import Registry
from .translate import text_of

KEY_PREFIX = "antigravity-ledger:"
USER = 0
AGENT = 1


def key(session_id: str) -> str:
    return f"{KEY_PREFIX}{session_id}"


@dataclass(slots=True)
class Ledger:
    tools: set[str] = field(default_factory=set)
    # The last tool the conversation reached; texts are counted from it.
    anchor: str = ""
    counts: dict[str, list[int]] = field(default_factory=dict)
    # When the device last saw `agy` write the conversation, in protocol milliseconds.
    wrote: int | None = None

    @classmethod
    def load(cls, registry: Registry, session_id: str) -> Ledger:
        raw = registry.get_kv(key(session_id))
        try:
            data = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            data = {}
        if not isinstance(data, dict):
            return cls()
        tools = data.get("tools")
        counts = data.get("counts")
        wrote = data.get("wrote")
        return cls(
            tools={str(item) for item in tools} if isinstance(tools, list) else set(),
            anchor=str(data.get("anchor") or ""),
            counts={
                str(anchor): [int(pair[0]), int(pair[1])]
                for anchor, pair in (counts.items() if isinstance(counts, dict) else [])
                if isinstance(pair, list) and len(pair) == 2
            },
            wrote=wrote if isinstance(wrote, int) and not isinstance(wrote, bool) else None,
        )

    def save(self, registry: Registry, session_id: str) -> None:
        data = {
            "tools": sorted(self.tools),
            "anchor": self.anchor,
            "counts": self.counts,
            "wrote": self.wrote,
        }
        registry.set_kv(key(session_id), json.dumps(data, separators=(",", ":")))

    def note_tool(self, call_id: str) -> None:
        """A tool the device published: texts after it are counted from it."""
        self.tools.add(call_id)
        self.anchor = call_id

    def note_text(self, role: int) -> None:
        """A message of the person's, or a text of `agy`'s, published where the conversation is."""
        self.counts.setdefault(self.anchor, [0, 0])[role] += 1


@dataclass(slots=True)
class Record:
    """A conversation's ledger, and the registry it is kept in under the session's id."""

    registry: Registry
    session_id: Callable[[], str | None]
    ledger: Ledger = field(default_factory=Ledger)

    def save(self) -> None:
        session_id = self.session_id()
        if session_id is not None:
            self.ledger.save(self.registry, session_id)

    def note_text(self, role: int) -> None:
        self.ledger.note_text(role)
        self.save()

    def note_tool(self, call_id: str) -> None:
        self.ledger.note_tool(call_id)
        self.save()


@dataclass(slots=True)
class Row:
    """One replayed row the device has not published: `user`, `agent` or `tool`."""

    kind: str
    text: str = ""
    attachments: list[dict[str, Any]] = field(default_factory=list)
    update: dict[str, Any] = field(default_factory=dict)


class Replay:
    """Walks one replay against the ledger, keeping the ledger up to date as it goes."""

    def __init__(self, ledger: Ledger) -> None:
        self._ledger = ledger
        self._anchor = ""
        self._seen: dict[str, list[int]] = {}
        self._user: list[Any] | None = None

    def user(self, content: Any) -> list[Row]:
        """A part of the person's message; the message is one row however many parts it has."""
        if self._user is None:
            self._user = []
        self._user.append(content)
        return []

    def agent(self, text: str) -> list[Row]:
        rows = self.finish()
        if self._new_text(AGENT):
            rows.append(Row("agent", text=text.lstrip("\n")))
        return rows

    def tool(self, update: dict[str, Any]) -> list[Row]:
        rows = self.finish()
        call_id = str(update.get("toolCallId") or "")
        if not call_id:
            return rows
        self._anchor = call_id
        known = call_id in self._ledger.tools
        self._ledger.note_tool(call_id)
        if not known:
            rows.append(Row("tool", update=update))
        return rows

    def finish(self) -> list[Row]:
        """The person's message the replay was in the middle of, now that something else came."""
        parts, self._user = self._user, None
        if parts is None or not self._new_text(USER):
            return []
        return [Row("user", text=_words(parts), attachments=_files(parts))]

    def _new_text(self, role: int) -> bool:
        seen = self._seen.setdefault(self._anchor, [0, 0])
        place = seen[role]
        seen[role] += 1
        published = self._ledger.counts.setdefault(self._anchor, [0, 0])
        if place < published[role]:
            return False
        published[role] = place + 1
        return True


def _words(parts: list[Any]) -> str:
    return "".join(text_of(part) for part in parts)


def _files(parts: list[Any]) -> list[dict[str, Any]]:
    """The files of a replayed message, by name: a path never reaches an app (4.8)."""
    found: list[dict[str, Any]] = []
    for part in parts:
        if not isinstance(part, dict):
            continue
        if part.get("type") == "resource_link":
            uri = str(part.get("uri") or "")
            name = str(part.get("name") or "") or _name(uri)
            size = part.get("size")
            found.append(
                {
                    "name": name,
                    "mime": str(part.get("mimeType") or "")
                    or mimetypes.guess_type(name)[0]
                    or "application/octet-stream",
                    "size": size if isinstance(size, int) and not isinstance(size, bool) else 0,
                }
            )
        elif part.get("type") == "resource" and isinstance(part.get("resource"), dict):
            name = _name(str(part["resource"].get("uri") or ""))
            mime = mimetypes.guess_type(name)[0] or "text/plain"
            found.append({"name": name, "mime": mime, "size": 0})
    return found


def _name(uri: str) -> str:
    path = unquote(urlsplit(uri).path) if "://" in uri else uri
    return os.path.basename(path.rstrip("/")) or "file"
