"""`agy`'s conversations as sessions of the device's, from its own list (A64, 4.4).

A conversation the device does not have yet is published as `agy` lists it — `origin` `terminal`,
`control` `none`, `idle`, with its title, its directory and the time of its last write — and goes
to the Archive; its transcript stays empty until a message continues it. Like the other mirrors the
device takes on the newest of them that `[mirror]` allows: by default the 50 newest of the last 14
days. Later lists move the title, the directory and the time of one it has but does not drive,
without making that activity of its own. A conversation missing from two lists in a row is
withdrawn, unless the device holds it. One `agy` keeps without a directory is published under the
person's home directory.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING

from ...attachments import without_paths
from ...config import MirrorConfig
from ...logging_setup import logger
from ...models import Session, now_ms, title_from_text
from ...sessions import titles
from .listing import Listed

if TYPE_CHECKING:  # pragma: no cover - imported for types only
    from ...sessions.hub import SessionEntry, SessionHub

log = logger("rc_client.antigravity.conversations")

AGENT = "antigravity"
MISSING_LISTS = 2
DAY_MS = 86_400_000


def published_cwd(listed: Listed) -> str:
    """Where a listed conversation is shown: its directory, or home when it has none."""
    return str(Path.home()) if listed.no_directory else listed.cwd


def clean_title(text: str) -> str:
    return title_from_text(without_paths(text))


class Conversations:
    """Applies each of `agy`'s lists to the hub."""

    def __init__(self, hub: SessionHub, limits: MirrorConfig) -> None:
        self.hub = hub
        self._limits = limits
        self._missing: dict[str, int] = {}
        # The conversations a list has named since the daemon started.
        self._named: set[str] = set()

    async def apply(self, listed: dict[str, Listed]) -> None:
        cutoff = now_ms() - self._limits.max_age_days * DAY_MS
        newest = sorted(
            (item for item in listed.values() if item.updated_ms >= cutoff),
            key=lambda item: item.updated_ms,
            reverse=True,
        )[: self._limits.max_sessions]
        for item in newest:
            if item.session_id not in self.hub.entries:
                await self._adopt(item)
        for item in listed.values():
            entry = self.hub.entries.get(item.session_id)
            if entry is not None and entry.session.agent == AGENT:
                await self._follow(entry, item)
        self._named.update(listed)
        await self._withdraw(self._ours(), listed)

    def _ours(self) -> list[SessionEntry]:
        return [entry for entry in self.hub.entries.values() if entry.session.agent == AGENT]

    async def _adopt(self, item: Listed) -> None:
        moment = item.updated_ms or now_ms()
        entry = self.hub.register_mirrored(
            Session(
                session_id=item.session_id,
                device_id=self.hub.device_id,
                agent=AGENT,
                cwd=published_cwd(item),
                title=clean_title(item.title),
                state="idle",
                origin="terminal",
                control="none",
                created_at=moment,
                updated_at=moment,
            )
        )
        await entry.channel.publish_summary(touch=False)

    async def _follow(self, entry: SessionEntry, item: Listed) -> None:
        """The title, directory and last write `agy` lists, for a conversation not driven here."""
        if entry.runner is not None:
            return
        session = entry.session
        changed = False
        cwd = published_cwd(item)
        if cwd and cwd != session.cwd:
            session.cwd, changed = cwd, True
        if item.updated_ms > session.updated_at:
            session.updated_at, changed = item.updated_ms, True
        title = clean_title(item.title)
        registry = entry.channel.registry
        own = titles.pinned(registry, session.session_id) or titles.named_after_files(
            registry, session.session_id
        )
        if title and title != session.title and not own:
            # A `meta`, then the summary with the other changes too; the row keeps its place.
            await entry.channel.set_title(title)
        elif changed:
            await entry.channel.publish_summary(touch=False)

    async def _withdraw(self, ours: Iterable[SessionEntry], listed: dict[str, Listed]) -> None:
        for entry in ours:
            session_id = entry.session.session_id
            if session_id in listed:
                self._missing.pop(session_id, None)
                continue
            if entry.session.origin != "terminal" and session_id not in self._named:
                continue
            missing = self._missing.get(session_id, 0) + 1
            self._missing[session_id] = missing
            if missing < MISSING_LISTS or entry.runner is not None or self.hub.holds(entry):
                continue
            self._missing.pop(session_id, None)
            self._named.discard(session_id)
            log.info("withdrew a conversation agy no longer lists")
            await self.hub.withdraw(entry)
