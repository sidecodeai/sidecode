"""Mirror agent sessions that were started in a terminal on this device.

Discovery re-scans every 10 s; live files are tailed every 2 s. A session the
daemon drives itself is never ingested from disk, so events are never doubled.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from dataclasses import dataclass, field
from typing import Any

from ..agents.claude import subagents, transcripts
from ..agents.claude.holders import SessionRef, scan_holders
from ..agents.codex import rollouts
from ..agents.codex.daemon.service import CodexDaemonService
from ..agents.codex.runtime import resolve_binary as resolve_codex
from ..agents.grok import cursor as grok_cursor
from ..agents.grok import sessions as grok_sessions
from ..agents.grok.runtime import resolve_binary as resolve_grok
from ..agents.grok.service import GrokLeaderService
from ..agents.grok.translate import SETTINGS as GROK_SETTINGS
from ..config import MirrorConfig
from ..logging_setup import logger
from ..models import Session, now_ms
from ..procscan import file_writers
from . import titles
from .hub import SessionEntry, SessionHub
from .limits import TurnEnd

log = logger("rc_client.mirror")

SCAN_INTERVAL = 10.0
TAIL_INTERVAL = 2.0
BACKFILL_BYTES = 4 * 1024 * 1024

Tailer = transcripts.TranscriptTailer | rollouts.RolloutTailer | grok_sessions.GrokTailer


def _turn_trigger(tailer: Tailer) -> str:
    """What the rows just read say started the turn (amendment A30).

    Only Claude sessions are shared with a terminal, and only their transcripts
    carry a message another agent filed as a user turn.
    """
    if isinstance(tailer, transcripts.TranscriptTailer):
        return tailer.turn_trigger
    return "terminal"


def _turn_end(tailer: Tailer) -> TurnEnd:
    """How the rows just read say the turn ended (amendments A32, A35).

    Only a Claude transcript records the marker the CLI leaves where the person
    interrupted a turn, and the 429 row it writes when the vendor's usage limit
    stopped one; every other tailer lets its turns run out.
    """
    if isinstance(tailer, transcripts.TranscriptTailer):
        return TurnEnd(tailer.stop_reason, tailer.limit)
    return TurnEnd()


@dataclass(slots=True)
class ClaudeMirror:
    """One mirrored Claude session: its transcript, and the subagents it started.

    `working` is the last answer the watch gave. The last subagent finishing
    writes nothing to the parent's transcript that the mirror can count on, so
    the flag is what turns that into an edge worth publishing.
    """

    tailer: transcripts.TranscriptTailer
    working: bool = False
    subagents: subagents.SubagentWatch = field(init=False)

    def __post_init__(self) -> None:
        self.subagents = subagents.SubagentWatch.for_transcript(self.tailer.path)

    @property
    def busy(self) -> bool:
        """Working means all of it: this session's own turn, or a subagent's."""
        return self.tailer.awaiting_reply or self.working


@dataclass(slots=True)
class CodexMirror:
    tailer: rollouts.RolloutTailer


@dataclass(slots=True)
class GrokMirror:
    tailer: grok_sessions.GrokTailer


class MirrorService:
    def __init__(
        self,
        hub: SessionHub,
        limits: MirrorConfig | None = None,
        *,
        codex_daemon: CodexDaemonService | None = None,
        grok_leader: GrokLeaderService | None = None,
    ) -> None:
        self.hub = hub
        self.limits = limits or MirrorConfig()
        self.codex_daemon = codex_daemon
        self.grok_leader = grok_leader
        self._claude: dict[str, ClaudeMirror] = {}
        self._codex: dict[str, CodexMirror] = {}
        self._grok: dict[str, GrokMirror] = {}
        # Title-only tails for Claude sessions this device drives itself.
        self._titles: dict[str, transcripts.TitleTail] = {}
        # Sessions whose transcript has already been read once for the settings
        # in force (A17); the backfill costs a whole file and is worth doing once.
        self._settings_read: set[str] = set()
        self._tasks: list[asyncio.Task[None]] = []

    def start(self) -> None:
        self._tasks = [
            asyncio.create_task(self._scan_loop()),
            asyncio.create_task(self._tail_loop()),
        ]

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks = []

    # ---------------------------------------------------------------- scans

    async def _scan_loop(self) -> None:
        # Before the first scan, and on the same task, so the settings of a
        # session the hub starts with are read exactly once.
        try:
            await self._backfill_known()
        except Exception:
            log.exception("terminal session settings backfill failed")
        while True:
            try:
                await self.scan_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("terminal session scan failed")
            await asyncio.sleep(SCAN_INTERVAL)

    async def scan_once(self) -> None:
        """Re-scan the agent state directories and refresh who controls what.

        Only the filesystem walk runs off the loop: registering a mirror
        creates tasks, so it must happen on the event loop thread.
        """
        found_claude = await asyncio.to_thread(
            transcripts.discover, self.limits.max_sessions, self.limits.max_age_days
        )
        found_codex = await asyncio.to_thread(
            rollouts.discover, self.limits.max_sessions, self.limits.max_age_days
        )
        found_grok = await asyncio.to_thread(
            grok_sessions.discover, self.limits.max_sessions, self.limits.max_age_days
        )
        if self.codex_daemon is not None:
            await self.codex_daemon.tick(resolve_codex())
        if self.grok_leader is not None:
            await self.grok_leader.tick(resolve_grok())
        adopted = self._adopt_claude(found_claude)
        # After the adoption, so a session whose transcript was just found is
        # never mistaken for one that never had one.
        await self.hub.sweep_ghosts()
        self._adopt_codex(found_codex)
        await self._adopt_grok(found_grok)
        await self._watch_titles()
        await self._refresh_claude_control()
        await self._refresh_codex_control()
        await self._refresh_grok_control()
        await self._backfill_settings(adopted)

    def _driven_claude(self) -> set[str]:
        """Claude sessions this device is running right now.

        Their events come from the SDK, so the mirror never adopts them; the
        title the CLI writes for itself reaches nobody unless it is read here.
        """
        return {
            session_id
            for session_id, entry in self.hub.entries.items()
            if entry.session.agent == "claude"
            and entry.session.origin == "remote"
            and entry.runner is not None
        }

    async def _watch_titles(self) -> None:
        """Keep one title-only tail per session this device drives."""
        wanted = self._driven_claude()
        for session_id in list(self._titles):
            if session_id not in wanted:
                self._titles.pop(session_id, None)
        for session_id in sorted(wanted - set(self._titles)):
            path = await asyncio.to_thread(transcripts.find_transcript, session_id)
            if path is not None:
                self._titles[session_id] = transcripts.TitleTail(path=str(path))

    async def _read_titles(self) -> None:
        """Publish any title the CLI has written since the last read."""
        for session_id, tail in list(self._titles.items()):
            entry = self.hub.entries.get(session_id)
            if entry is None:
                self._titles.pop(session_id, None)
                continue
            for title in await asyncio.to_thread(tail.read_new):
                await self._apply_title(entry, title)

    @staticmethod
    async def _apply_title(entry: SessionEntry, title: transcripts.Title) -> None:
        """A `/rename` in the terminal is as sticky as one set from an app."""
        if title.by_user:
            await titles.from_user(entry.channel, title.text)
        else:
            await titles.from_agent(entry.channel, title.text)

    def _offset_key(self, session_id: str) -> str:
        return f"mirror-offset:{session_id}"

    def _initial_offset(self, session_id: str, path: str) -> int:
        stored = self.hub.registry.get_kv(self._offset_key(session_id))
        if stored is not None and stored.isdigit():
            return int(stored)
        try:
            size = os.path.getsize(path)
        except OSError:
            return 0
        return max(0, size - BACKFILL_BYTES)

    def _adopt_claude(self, found: list[transcripts.TranscriptInfo]) -> list[str]:
        adopted: list[str] = []
        for info in found:
            if info.session_id in self._claude:
                continue
            entry = self.hub.entries.get(info.session_id)
            if entry is not None and not self._adoptable(entry):
                continue
            if entry is None:
                entry = self.hub.register_mirrored(
                    Session(
                        session_id=info.session_id,
                        device_id=self.hub.device_id,
                        agent="claude",
                        cwd=info.cwd,
                        title="",
                        state="idle",
                        origin="terminal",
                        control="none",
                        created_at=int(info.mtime * 1000),
                        updated_at=int(info.mtime * 1000),
                    ),
                    transcript=info.path,
                )
            tailer = transcripts.TranscriptTailer(path=info.path, cwd=info.cwd)
            tailer.offset = self._initial_offset(info.session_id, info.path)
            entry.transcript = info.path
            entry.channel.start()
            self._claude[info.session_id] = ClaudeMirror(tailer=tailer)
            adopted.append(info.session_id)
        return adopted

    # ------------------------------------------------------------- settings

    async def _backfill_known(self) -> None:
        """Read the settings of every mirrored Claude session the hub starts with.

        A session restored from the registry has no mirror yet, and one whose
        transcript is too old to be re-discovered never gets one, so this pass is
        the only place their model, permission mode and effort are read.
        """
        await self._backfill_settings(
            [
                session_id
                for session_id, entry in self.hub.entries.items()
                if entry.session.agent == "claude" and self._adoptable(entry)
            ]
        )

    async def _backfill_settings(self, session_ids: list[str]) -> None:
        """Publish what the terminal chose, from one whole-file read per session."""
        for session_id in session_ids:
            entry = self.hub.entries.get(session_id)
            if entry is None or session_id in self._settings_read:
                continue
            if not self._adoptable(entry):
                continue
            path = await self._transcript_path(entry, session_id)
            if path is None:
                continue
            self._settings_read.add(session_id)
            settings = await asyncio.to_thread(transcripts.latest_settings, path)
            if settings:
                await entry.channel.set_meta(**settings)

    @staticmethod
    async def _transcript_path(entry: SessionEntry, session_id: str) -> str | None:
        if entry.transcript is not None:
            return entry.transcript
        found = await asyncio.to_thread(transcripts.find_transcript, session_id)
        return str(found) if found is not None else None

    def _codex_is_daemons(self, thread_id: str) -> bool:
        """A thread the shared daemon knows is read from the daemon, never from disk.

        What is left for the rollout mirror is exactly A11's fourth row: a TUI
        started with configuration overrides, which runs its own embedded
        app-server and is invisible to the daemon.
        """
        return self.codex_daemon is not None and self.codex_daemon.knows(thread_id)

    def _grok_is_leaders(self, session_id: str) -> bool:
        """A session the leader holds is read from the leader, never from disk.

        What is left for the update-log mirror is A28's third row: a `grok`
        started with `use_leader` off or under a sandbox profile, which runs its
        own agent and is invisible to the leader.
        """
        return self.grok_leader is not None and self.grok_leader.knows(session_id)

    def _adopt_codex(self, found: list[rollouts.RolloutInfo]) -> None:
        for info in found:
            if info.thread_id in self._codex or self._codex_is_daemons(info.thread_id):
                continue
            entry = self.hub.entries.get(info.thread_id)
            if entry is not None and not self._adoptable(entry):
                continue
            if entry is None:
                entry = self.hub.register_mirrored(
                    Session(
                        session_id=info.thread_id,
                        device_id=self.hub.device_id,
                        agent="codex",
                        cwd=info.cwd,
                        title="",
                        state="idle",
                        origin="terminal",
                        control="none",
                        created_at=int(info.mtime * 1000),
                        updated_at=int(info.mtime * 1000),
                    ),
                    transcript=info.path,
                )
            tailer = rollouts.RolloutTailer(path=info.path, cwd=info.cwd)
            tailer.offset = self._initial_offset(info.thread_id, info.path)
            entry.transcript = info.path
            entry.channel.start()
            self._codex[info.thread_id] = CodexMirror(tailer=tailer)

    async def _adopt_grok(self, found: list[grok_sessions.GrokSessionInfo]) -> None:
        """Take on a Grok session from the update log it keeps for itself.

        Grok names its own sessions, so the title and the settings in force are
        read straight out of `summary.json` rather than waited for.
        """
        for info in found:
            if info.session_id in self._grok or self._grok_is_leaders(info.session_id):
                continue
            entry = self.hub.entries.get(info.session_id)
            if entry is not None and not self._adoptable(entry):
                continue
            if entry is None:
                entry = self.hub.register_mirrored(
                    Session(
                        session_id=info.session_id,
                        device_id=self.hub.device_id,
                        agent="grok",
                        cwd=info.cwd,
                        title="",
                        state="idle",
                        origin="terminal",
                        control="none",
                        created_at=int(info.mtime * 1000),
                        updated_at=int(info.mtime * 1000),
                    ),
                    transcript=info.path,
                )
            tailer = grok_sessions.GrokTailer(path=info.path, cwd=info.cwd)
            tailer.offset = self._initial_offset(info.session_id, info.path)
            tailer.resume_from = grok_cursor.read(self.hub.registry, info.session_id)
            entry.transcript = info.path
            entry.channel.start()
            self._grok[info.session_id] = GrokMirror(tailer=tailer)
            if info.settings:
                await entry.channel.set_meta(**info.settings)
            if info.title:
                await titles.from_agent(entry.channel, info.title)

    async def _refresh_grok_control(self) -> None:
        """Grok's own registry says who is live; the file's writers are the fallback."""
        active, known = await asyncio.to_thread(grok_sessions.active_ids)
        for session_id, mirror in list(self._grok.items()):
            if self._grok_is_leaders(session_id):
                self._grok.pop(session_id, None)
                continue
            entry = self._live_entry(session_id, self._grok)
            if entry is None:
                continue
            if known and session_id in active:
                owned = True
            else:
                writers, seen = await file_writers(mirror.tailer.path)
                if not seen:
                    continue
                owned = bool([pid for pid in writers if pid != os.getpid()])
            await self._set_control(entry, "terminal" if owned else "none", mirror.tailer.running)

    @staticmethod
    def _adoptable(entry: SessionEntry) -> bool:
        """Whether the transcript on disk is someone else's to read.

        A session we drive writes its own transcript, so re-reading it would
        replay the whole conversation as terminal messages with fresh block ids.
        `origin` is persisted, which is what makes this survive a restart, when
        the runner is gone but the session is still ours.
        """
        return entry.runner is None and entry.session.origin != "remote"

    def _live_entry(self, session_id: str, mirrors: dict[str, Any]) -> SessionEntry | None:
        """Re-resolve the hub entry, dropping the mirror when it is no longer ours.

        The hub replaces the entry object on rekey and removes it on delete, so
        holding the object would keep a deleted session alive and would fight a
        live runner for the same id.
        """
        entry = self.hub.entries.get(session_id)
        if entry is None or not self._adoptable(entry):
            mirrors.pop(session_id, None)
            return None
        return entry

    def _claude_tracked(self) -> list[tuple[str, SessionEntry, ClaudeMirror | None]]:
        """Every Claude session whose owner has to be re-checked this round.

        Sessions with a transcript come from the mirror; a session an attachment
        created before its first turn has no transcript yet, and would otherwise
        never learn that its CLI has gone.
        """
        tracked: list[tuple[str, SessionEntry, ClaudeMirror | None]] = []
        for session_id, mirror in list(self._claude.items()):
            entry = self._live_entry(session_id, self._claude)
            if entry is not None:
                tracked.append((session_id, entry, mirror))
        seen = {session_id for session_id, _, _ in tracked}
        for session_id, entry in list(self.hub.entries.items()):
            if session_id in seen or entry.session.agent != "claude":
                continue
            if entry.shared is None and entry.session.control != "terminal":
                continue
            if entry.runner is None:
                tracked.append((session_id, entry, None))
        return tracked

    async def _refresh_claude_control(self) -> None:
        """Pair every tracked session with the terminal process running it, if any.

        The whole round is assigned at once so one process cannot be handed to
        several sessions: an attached session claims its own CLI by the pid the
        bridge registered, which is what keeps the other sessions in the same
        directory out of it.
        """
        tracked = self._claude_tracked()
        if not tracked:
            return
        scan = await scan_holders()
        assigned = scan.assign(
            [
                SessionRef(
                    session_id,
                    cwd=entry.session.cwd,
                    pid=entry.holder_pid,
                    identity=entry.holder_identity,
                )
                for session_id, entry, _ in tracked
            ]
        )
        for session_id, entry, mirror in tracked:
            running = mirror.busy if mirror is not None else False
            if entry.shared is not None:
                tailer = mirror.tailer if mirror is not None else None
                trigger = tailer.turn_trigger if tailer is not None else "terminal"
                end = _turn_end(tailer) if tailer is not None else TurnEnd()
                await self.hub.shared.tick(entry, running, trigger, end)
                continue
            if not scan.complete:
                continue
            holder = assigned.get(session_id)
            if holder is not None:
                entry.holder_pid = holder.pid
                entry.holder_identity = holder.identity
                await self._set_control(entry, "terminal", running)
            else:
                entry.holder_pid = None
                entry.holder_identity = None
                if mirror is not None:
                    # The CLI is gone, and so is everything it was running.
                    mirror.tailer.awaiting_reply = False
                    mirror.working = False
                await self._set_control(entry, "none", False)

    async def _refresh_codex_control(self) -> None:
        for session_id, mirror in list(self._codex.items()):
            if self._codex_is_daemons(session_id):
                self._codex.pop(session_id, None)
                continue
            entry = self._live_entry(session_id, self._codex)
            if entry is None:
                continue
            writers, known = await file_writers(mirror.tailer.path)
            if not known:
                continue
            owned = bool([pid for pid in writers if pid != os.getpid()])
            await self._set_control(entry, "terminal" if owned else "none", mirror.tailer.running)

    async def _set_control(self, entry: SessionEntry, control: str, running: bool) -> None:
        """Amendment A7: `readonly` only while the terminal holds an idle session."""
        changed = entry.session.control != control
        if changed:
            entry.session.control = control  # type: ignore[assignment]
            await entry.channel.emit("meta", control=control)
        if control == "terminal":
            await entry.channel.set_state("running" if running else "readonly")
        else:
            # An archived session nothing has revived reads back as `stopped`
            # (`hub.load`), and a scan that finds no terminal has learnt nothing
            # that would change that.
            await entry.channel.set_state("stopped" if entry.session.archived else "idle")
        if changed:
            await entry.channel.publish_summary()

    # ----------------------------------------------------------------- tails

    async def _tail_loop(self) -> None:
        while True:
            await asyncio.sleep(TAIL_INTERVAL)
            try:
                await self.tail_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("terminal session tail failed")

    async def tail_once(self) -> None:
        for session_id, claude_mirror in list(self._claude.items()):
            entry = self._live_entry(session_id, self._claude)
            if entry is None:
                continue
            await self._tail_claude(session_id, entry, claude_mirror)
        for session_id, codex_mirror in list(self._codex.items()):
            if self._codex_is_daemons(session_id):
                self._codex.pop(session_id, None)
                continue
            entry = self._live_entry(session_id, self._codex)
            if entry is None:
                continue
            await self._tail_one(session_id, entry, codex_mirror.tailer)
        for session_id, grok_mirror in list(self._grok.items()):
            if self._grok_is_leaders(session_id):
                self._grok.pop(session_id, None)
                continue
            entry = self._live_entry(session_id, self._grok)
            if entry is None:
                continue
            await self._tail_one(session_id, entry, grok_mirror.tailer)
        await self._read_titles()

    async def _tail_one(self, session_id: str, entry: SessionEntry, tailer: Tailer) -> None:
        """Read what is new, then report turn state as those rows left it.

        Reading the flag first would report the previous pass's state, which
        closes a turn one tail interval after the message that started it.
        """
        rows = await asyncio.to_thread(tailer.read_new)
        if not rows:
            return
        await self._apply_rows(session_id, entry, tailer, rows)
        await self._after_rows(entry, tailer.busy, _turn_trigger(tailer), _turn_end(tailer))

    async def _tail_claude(
        self, session_id: str, entry: SessionEntry, mirror: ClaudeMirror
    ) -> None:
        """The same, plus the subagents the session started (working means all of it).

        The subagents are asked even when the transcript has not grown: the last
        one finishing is what ends the turn, and nothing the parent writes says
        so. Asking them is a `stat` per file, which is why it runs every tail
        interval; only a change of answer is published, so a session with work
        under way is not a summary every two seconds.
        """
        rows = await asyncio.to_thread(mirror.tailer.read_new)
        working = await asyncio.to_thread(mirror.subagents.working)
        changed = working != mirror.working
        mirror.working = working
        if not rows and not changed:
            return
        if rows:
            await self._apply_rows(session_id, entry, mirror.tailer, rows)
        await self._after_rows(
            entry, mirror.busy, _turn_trigger(mirror.tailer), _turn_end(mirror.tailer)
        )

    async def _apply_rows(
        self, session_id: str, entry: SessionEntry, tailer: Tailer, rows: list[dict[str, Any]]
    ) -> None:
        for row in rows:
            for emit in tailer.translate(row):
                await self._apply(entry, emit)
        self.hub.registry.set_kv(self._offset_key(session_id), str(tailer.offset))
        if isinstance(tailer, grok_sessions.GrokTailer):
            # Grok's `eventId` counter resumes a tail even when the file moved,
            # and is the same mark the leader reads when it joins (A28).
            grok_cursor.write(self.hub.registry, session_id, tailer.cursor)

    async def _after_rows(
        self, entry: SessionEntry, running: bool, trigger: str, end: TurnEnd | None = None
    ) -> None:
        entry.session.updated_at = now_ms()
        if entry.shared is not None:
            await self.hub.shared.tick(entry, running, trigger, end)
        elif entry.session.control == "terminal":
            await entry.channel.set_state("running" if running else "readonly")
        await entry.channel.publish_summary()

    async def _apply(self, entry: SessionEntry, emit: Any) -> None:
        if emit.kind in _CHANNEL_ECHOES:
            await self._channel_echo(entry, emit)
            return
        if emit.kind == transcripts.TITLE:
            await self._apply_title(
                entry,
                transcripts.Title(
                    text=str(emit.fields.get("title") or ""),
                    by_user=bool(emit.fields.get("by_user")),
                ),
            )
            return
        if emit.kind in {transcripts.SESSION_SETTINGS, GROK_SETTINGS}:
            # Amendment A17: what the terminal chose, from its own records.
            # `set_meta` publishes only the fields that actually changed.
            await entry.channel.set_meta(**emit.fields)
            return
        if emit.kind == transcripts.QUESTION_ANSWERED:
            # Amendment A20: the terminal answered its own dialog first.
            if entry.shared is not None:
                await self.hub.shared.question_answered(entry, emit.fields.get("answers"))
            return
        if emit.kind == transcripts.COMMAND_OUTPUT:
            # Amendment A40: what the CLI said back, which is what confirms a
            # command the device typed. Never a block, whoever typed it.
            if entry.shared is not None:
                await self.hub.shared.command_output(entry, str(emit.fields.get("text") or ""))
            return
        if entry.shared is not None and emit.kind == "tool_call":
            status = str(emit.fields.get("status") or "")
            if status in {"succeeded", "failed"}:
                await self.hub.shared.tool_finished(
                    entry, str(emit.fields.get("tool") or ""), status == "failed"
                )
        if emit.kind == "todos":
            await entry.channel.publish_todos(list(emit.fields.get("items") or []))
            return
        # Amendment A40: a `terminal` row may be the device's own keystrokes
        # coming back. A command it typed is not the person speaking, and the
        # app already has the bubble it asked for.
        if (
            emit.kind == "user_message"
            and emit.fields.get("source") == "terminal"
            and entry.shared is not None
            and await self.hub.shared.typed_command(entry, str(emit.fields.get("text") or ""))
        ):
            return
        if emit.kind == "user_message" and emit.fields.get("source") != "agent":
            # A session is named after its first message, and a teammate's
            # report is not one the person sent (amendment A30).
            await titles.from_prompt(entry.channel, str(emit.fields.get("text") or ""))
        await entry.channel.emit(emit.kind, **emit.fields)

    async def _channel_echo(self, entry: SessionEntry, emit: Any) -> None:
        """Internal transcript signals about our own injections; never published."""
        message_id = str(emit.fields.get("message_id") or "")
        if not message_id:
            return
        if emit.kind == transcripts.CHANNEL_DELIVERED:
            await self.hub.shared.echo_delivered(entry, message_id)
        else:
            await self.hub.shared.echo_absorbed(entry, message_id)


_CHANNEL_ECHOES = {transcripts.CHANNEL_DELIVERED, transcripts.CHANNEL_ABSORBED}
