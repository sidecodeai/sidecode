"""The device's view of the shared Codex daemon: one connection, many sessions.

The daemon is the source of truth for every Codex thread on this machine, so
when it answers a handshake this replaces rollout mirroring outright: history
comes from `thread/list`, live threads from `thread/loaded/list`, and the block
timeline from the notification stream.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import time
from collections import deque
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from ....errors import RcError
from ....logging_setup import logger
from ....models import Session
from ....sessions import titles
from ..account import RATE_LIMITS
from ..models import ModelCatalog, catalog_cache
from . import dialogs, terminals, threads
from .children import ChildIndex
from .repair import DaemonRepair
from .rpc import DaemonClient, NoReply
from .session import CodexDaemonSession
from .transport import socket_exists

if TYPE_CHECKING:  # pragma: no cover - imported for types only
    from ....sessions.hub import SessionEntry, SessionHub

log = logger("rc_client.codex.daemon.service")

ModeCallback = Callable[[], Awaitable[None]]
TerminalScanner = Callable[[], Awaitable[terminals.TerminalScan]]

HISTORY_LIMIT = 100
THREAD_CONFIG_ENV = "RC_CODEX_THREAD_CONFIG"
# Threads seen opened but still empty. One per TUI that is started and not
# typed into, so a handful covers a working day and the oldest may be dropped.
QUIET_THREADS = 64
# Threads another application owns, remembered so the index stops asking about
# them. The whole machine's history goes through here, so this is generous.
FOREIGN_THREADS = 256
# How long a thread the device is closing stays out of reach (A39). Codex
# answers `thread/archive` with `thread/archived`, which is what normally ends
# the window; this is the bound for a daemon that never does.
CLOSING_GRACE_S = 30.0


def thread_config() -> dict[str, Any] | None:
    """A per-thread `config` override for threads this device starts.

    Codex applies it to the thread wherever it is later resumed, including in a
    terminal, which is the only way to change a thread's configuration without
    editing the user's own files. Empty unless the device is told otherwise.
    """
    raw = os.environ.get(THREAD_CONFIG_ENV, "").strip()
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        log.warning("ignoring an unreadable thread config override", env=THREAD_CONFIG_ENV)
        return None
    return parsed if isinstance(parsed, dict) else None


def _unattended(session: CodexDaemonSession) -> bool:
    """Whether a request only a terminal could answer would wait for a person who is not there.

    What the apps cannot show is otherwise left unanswered, for the terminal. A
    turn started from an app on a thread no terminal is in has none, and
    unanswered, the request would stop the turn for good. A turn the terminal
    started is never called unattended. The scan that finds terminals is blind
    to some, and a terminal may not be known yet when Codex replays its open
    request to a thread just attached, so an answer would be given for a person
    who is there.
    """
    return session.local_turn and not session.terminal_holds


def _listed(answer: dict[str, Any]) -> bool:
    """Whether a daemon listing answered at all: one that failed is `{}`, with no list in it."""
    return isinstance(answer.get("data"), list)


def _whole_history(listing: dict[str, Any]) -> bool:
    """Whether a `thread/list` answer is the whole history, which Codex ends with no cursor.

    An answer that failed is no page at all, so never the whole history.
    """
    return _listed(listing) and not listing.get("nextCursor")


class CodexDaemonService:
    """Owns the daemon connection and maps its threads onto hub sessions."""

    def __init__(
        self,
        hub: SessionHub,
        version: str,
        on_mode_change: ModeCallback | None = None,
        scan_terminals: TerminalScanner | None = None,
        repair: DaemonRepair | None = None,
    ) -> None:
        self.hub = hub
        self._version = version
        self._on_mode_change = on_mode_change
        self._scan_terminals = scan_terminals or terminals.scan_terminals
        self._repair = repair or DaemonRepair()
        self._client: DaemonClient | None = None
        self._known: set[str] = set()
        # Threads the daemon says are loaded. A brand-new one is loaded before
        # its first turn is persisted, and `thread/resume` fails until then, so
        # "loaded" and "we have a runner" are two different facts.
        self._loaded: set[str] = set()
        # Loaded threads with nothing in them: a TUI opens one at startup and
        # may never use it. They become sessions when they first speak.
        self._quiet: deque[str] = deque(maxlen=QUIET_THREADS)
        # Threads A18 has already settled as somebody else's. Nothing ever
        # changes a thread's provenance, so one answer stands for the run and
        # a thread that keeps speaking costs no further `thread/read`.
        self._foreign: deque[str] = deque(maxlen=FOREIGN_THREADS)
        # When the daemon last saw each thread used. The session record's own
        # `updated_at` is the moment the apps last heard about the session,
        # which a title or a settings change moves for reasons of our own.
        self._used: dict[str, int] = {}
        # Threads a session of ours spawned a subagent in. They are never
        # sessions themselves (A18); their work is their parent's.
        self._children = ChildIndex()
        self._attaching: set[str] = set()
        # Threads the device is closing on the person's word, and when each
        # window expires (A39). Nothing a thread says while it is in here
        # adopts it, revives it or republishes it as alive.
        self._closing: dict[str, float] = {}
        self._catalog = ModelCatalog()
        self._config = thread_config()
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------- lifecycle

    @property
    def ready(self) -> bool:
        return self._client is not None and self._client.connected

    @property
    def client(self) -> DaemonClient:
        client = self._client
        if client is None:
            raise RcError("agent_unavailable", "the codex daemon is not connected")
        return client

    async def rate_limits(self) -> dict[str, Any]:
        """The account's rate-limit windows, where `/usage` reads them too (A33)."""
        return await self.client.request(RATE_LIMITS, {})

    def closing_thread(self, thread_id: str) -> None:
        """A session's close has begun (A39); hold the thread out of reach for it.

        The runner says so before it interrupts the turn, because everything the
        thread says between here and its `thread/closed` — the interrupted
        turn's completion, a status going idle, the close itself — would
        otherwise be read as a thread that is alive and adopted back.
        """
        self._closing[thread_id] = time.monotonic() + CLOSING_GRACE_S

    def is_closing(self, thread_id: str) -> bool:
        """Whether a close of this thread is still under way."""
        deadline = self._closing.get(thread_id)
        if deadline is None:
            return False
        if time.monotonic() >= deadline:
            del self._closing[thread_id]
            return False
        return True

    def knows(self, thread_id: str) -> bool:
        """Whether this thread is the daemon's, so the rollout mirror leaves it alone."""
        return thread_id in self._known

    def child_parent(self, thread_id: str) -> str | None:
        """The session a subagent's thread works for, when it is one of ours."""
        return self._children.parent(thread_id)

    async def start(self, binary: str | None) -> bool:
        """Try the daemon once. False means the device stays on the fallback path."""
        if not socket_exists():
            return False
        if binary:
            self._catalog = await catalog_cache.get(binary)
        client = DaemonClient(
            self._version,
            on_notification=self._notification,
            on_request=self._request,
            on_connected=self._reconnected,
        )
        try:
            await client.start()
        except Exception as exc:
            log.info("the codex daemon socket is present but did not answer", error=str(exc)[:200])
            with contextlib.suppress(Exception):
                await client.close()
            return False
        self._client = client
        await self.prune_foreign()
        await self.refresh()
        return True

    async def stop(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            await client.close()

    async def tick(self, binary: str | None) -> None:
        """Keep the daemon, the mode and the thread index current, on the scan interval.

        A daemon started here after the device started switches the mode without
        a restart; sessions already discovered on the fallback path keep the
        runner they have until they are next resumed.
        """
        if self.ready:
            await self.refresh()
            await self._repair.check_drift(self._restart_safe)
            return
        if self._client is not None:
            # The client is reconnecting on its own backoff.
            return
        # Nothing else on this machine ever starts the shared daemon, and
        # without one every terminal Codex is takeover-only.
        await self._repair.ensure_running()
        if await self.start(binary) and self._on_mode_change is not None:
            await self._on_mode_change()

    async def _restart_safe(self) -> bool:
        """Whether restarting the daemon right now would interrupt somebody.

        A restart drops every subscriber, so it waits for two things: a turn
        this device is driving, and any TUI the daemon is serving. The scan
        counts the TUIs that hold no rollout of their own, which are exactly the
        ones a restart would take the app-server away from; it fails closed, and
        an incomplete answer is not permission.
        """
        for entry in list(self.hub.entries.values()):
            runner = entry.runner
            if isinstance(runner, CodexDaemonSession) and (runner.busy or runner.terminal_holds):
                return False
        scan = await self._scan_terminals()
        return scan.complete and not scan.cwds

    # -------------------------------------------------------------- indexing

    async def refresh(self) -> None:
        """Reconcile the session list with the daemon's history and live threads."""
        if not self.ready:
            return
        epochs = {
            thread_id: entry.runner.activity_epoch
            for thread_id, entry in self.hub.entries.items()
            if isinstance(entry.runner, CodexDaemonSession)
        }
        child_updates: list[tuple[str, str, bool]] = []
        async with self._lock:
            # Most recently used first. Codex orders by creation unless told
            # otherwise, which pushes a thread started long ago off the page
            # however recently it was used. Every release from 0.153 takes the key.
            history = await self._safe_request(
                "thread/list",
                {"limit": HISTORY_LIMIT, "sortKey": "updated_at", "sortDirection": "desc"},
            )
            loaded = await self._safe_request("thread/loaded/list", {})
            live = {
                str(item) for item in (loaded.get("data") or []) if isinstance(item, str) and item
            }
            self._loaded = live
            page = threads.summaries(history.get("data"))
            for summary in page:
                self._known.add(summary.thread_id)
                await self._adopt(summary, loaded=summary.thread_id in live)
            for thread_id in live - self._known:
                await self._adopt_by_id(thread_id, child_updates=child_updates)
            if _listed(loaded):
                # Without the list of loaded threads nothing can be called gone: a
                # thread being driven may have no rollout to list yet.
                await self._forget_deleted(page, live, complete=_whole_history(history))
            await self.refresh_terminals()
        # Finishing a turn can drain its queue and start another daemon request.
        # Neither the index lock nor the session's notification lock may hold it.
        for summary in page:
            runner = self._session(summary.thread_id)
            if runner is not None and summary.active is True:
                await runner.reconcile_status(
                    True, epochs.get(summary.thread_id, runner.activity_epoch)
                )
        for parent_id, child_id, active in child_updates:
            runner = self._session(parent_id)
            if runner is not None:
                await runner.child_activity(child_id, active, epochs.get(parent_id, 0))
        for summary in page:
            runner = self._session(summary.thread_id)
            if runner is not None and summary.active is False:
                await runner.reconcile_status(
                    False, epochs.get(summary.thread_id, runner.activity_epoch)
                )
        await self._check_children()

    async def _check_children(self) -> None:
        """Let a turn end whose subagents have gone silent for good.

        Outside the index lock: ending a turn drains the session's queue, which
        starts the next turn and talks to the daemon on its own account.
        """
        for entry in list(self.hub.entries.values()):
            runner = entry.runner
            if isinstance(runner, CodexDaemonSession):
                await runner.check_children()

    async def refresh_terminals(self) -> None:
        """Decide which threads a terminal is in, on the scan interval.

        The daemon emits nothing when a TUI exits and never unloads a thread,
        so the TUI processes are the whole signal — and there are always fewer
        of them than there are loaded threads in the directory they run in. A
        directory with one live `codex` therefore hands its claim to one
        thread: the one that already had it, else the one used most recently.
        Everything else in that directory is a thread the user finished with.
        An incomplete scan changes nothing.
        """
        watched = [
            entry
            for entry in list(self.hub.entries.values())
            if isinstance(entry.runner, CodexDaemonSession)
            and entry.session.session_id in self._loaded
        ]
        if not watched:
            return
        scan = await self._scan_terminals()
        if not scan.complete:
            return
        rooms: dict[str, list[SessionEntry]] = {}
        for entry in watched:
            rooms.setdefault(entry.session.cwd, []).append(entry)
        for cwd, room in rooms.items():
            claims = scan.count(cwd)
            for entry in sorted(room, key=self._claim_order):
                runner = entry.runner
                if not isinstance(runner, CodexDaemonSession):  # pragma: no cover - narrowing
                    continue
                # A thread this device started is not one a terminal opened, so
                # the scan never hands it a claim until somebody has been seen
                # typing in it — after that a `codex resume` on it is as
                # ordinary as any other terminal.
                eligible = runner.terminal_seen or not self._created_here(entry)
                if runner.terminal_present(claims > 0 and eligible):
                    claims -= 1
                await self.publish_control(entry)

    async def _forget_deleted(
        self, page: list[threads.ThreadSummary], live: set[str], complete: bool
    ) -> None:
        """Drop sessions for threads deleted in Codex, as the mirror does for transcripts.

        A complete page is the whole history, so a thread missing from it is gone
        however long ago it was used. Otherwise `thread/list` is one page, most
        recently used first, and a thread missing from it may only have fallen
        off the end. Only a thread the daemon last saw used after the oldest
        entry on the page would have been on it, which is what makes its absence
        proof of deletion; one used at that very moment may have been cut off by
        the page size. That time is `_used`, in the clock the page is sorted by:
        the session record's own `updated_at` moves to now whenever the device
        publishes the session, so it says nothing about when Codex last saw the
        thread used. An empty page proves nothing, complete or not: it is what a
        daemon that listed nothing looks like, and silence is not evidence.
        """
        if not page:
            return
        seen = {summary.thread_id for summary in page}
        cutoff = None if complete else min(summary.updated_at for summary in page)
        for thread_id in sorted(self._known - seen - live):
            entry = self.hub.entries.get(thread_id)
            if entry is None:
                self._known.discard(thread_id)
                continue
            keeps_running = entry.runner is not None and not isinstance(
                entry.runner, CodexDaemonSession
            )
            last_used = self._used.get(thread_id, entry.session.updated_at)
            if keeps_running or (cutoff is not None and last_used <= cutoff):
                continue
            self._known.discard(thread_id)
            self._loaded.discard(thread_id)
            self._used.pop(thread_id, None)
            log.info("forgetting a codex thread deleted in the agent")
            await self.hub.delete({"session_id": thread_id})

    async def prune_foreign(self) -> None:
        """Drop sessions published for threads another application owns (A18).

        Everything adopted from now on is filtered as it arrives, so this runs
        once, on the connection the device starts with, and only over sessions
        the device is not driving. A thread the daemon cannot describe is left
        alone: silence is not evidence that it is somebody else's.
        """
        if not self.ready:
            return
        async with self._lock:
            dropped = 0
            for entry in list(self.hub.entries.values()):
                if not self._prunable(entry):
                    continue
                thread_id = entry.session.session_id
                result = await self._safe_request("thread/read", {"threadId": thread_id})
                thread = result.get("thread") if isinstance(result.get("thread"), dict) else result
                if not isinstance(thread, dict) or not thread or threads.is_ours(thread):
                    continue
                self._known.discard(thread_id)
                self._loaded.discard(thread_id)
                self._used.pop(thread_id, None)
                self._foreign.append(thread_id)
                await self.hub.withdraw(entry)
                dropped += 1
            if dropped:
                log.info("dropped codex threads another application owns", count=dropped)

    def _prunable(self, entry: SessionEntry) -> bool:
        """Whether A18 may still take this session away.

        A session this device created is its own whatever the index says, and
        one in the middle of a turn is being driven from an app right now.
        """
        if entry.session.agent != "codex" or entry.session.origin == "remote":
            return False
        runner = entry.runner
        if runner is None:
            return True
        return isinstance(runner, CodexDaemonSession) and not runner.busy

    async def _safe_request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        try:
            return await self.client.request(method, params)
        except RcError as exc:
            log.warning("codex daemon request failed", method=method, error=exc.message[:200])
            return {}

    async def _adopt_by_id(
        self,
        thread_id: str,
        *,
        spoke: bool = False,
        child_updates: list[tuple[str, str, bool]] | None = None,
    ) -> None:
        """Adopt one thread the index named rather than described.

        `spoke` is for a thread that has just sent a notification: it is in
        use, whatever the index still says about it, and the client using it
        is not this device, which drives only threads it already holds.
        """
        if thread_id in self._foreign or self.is_closing(thread_id):
            return
        result = await self._safe_request("thread/read", {"threadId": thread_id})
        thread = result.get("thread") if isinstance(result.get("thread"), dict) else result
        if not isinstance(thread, dict) or threads.is_ephemeral(thread):
            return
        if await self._adopt_child(thread, child_updates=child_updates):
            return
        summary = threads.ThreadSummary.parse(thread)
        if summary is None:
            return
        if not summary.ours:
            # A thread another application owns is foreign however loudly it
            # speaks: nothing this device can offer would be its to offer.
            self._foreign.append(summary.thread_id)
            return
        self._known.add(summary.thread_id)
        await self._adopt(summary, loaded=True, terminal=spoke, spoke=spoke)

    async def _adopt_child(
        self,
        thread: dict[str, Any],
        *,
        child_updates: list[tuple[str, str, bool]] | None = None,
    ) -> bool:
        """Take a thread one of our sessions spawned as that session's subagent.

        A subagent is never a session whoever spawned it (A18), and it is not
        somebody else's application either: it is work this device's own
        session started, so the parent stays green until it is done.
        """
        parent_id = threads.parent_of(thread)
        child_id = str(thread.get("id") or thread.get("sessionId") or "")
        if not child_id or parent_id is None or parent_id not in self.hub.entries:
            return False
        self._children.remember(child_id, parent_id)
        session = self._session(parent_id)
        status = thread.get("status")
        if isinstance(status, dict) and status.get("type") in {"active", "idle"}:
            active = threads.is_active(status)
            if child_updates is not None:
                child_updates.append((parent_id, child_id, active))
            elif session is not None:
                await session.child_activity(child_id, active)
        return True

    async def _adopt(
        self,
        summary: threads.ThreadSummary,
        *,
        loaded: bool,
        terminal: bool = False,
        spoke: bool = False,
    ) -> None:
        """Make one daemon thread a session, once there is a session to make.

        An empty thread is not one: a TUI opens a thread the moment it starts,
        long before anything is typed into it, and publishing that would put an
        untitled row in every app the moment a terminal window opens — one more
        for every window the user opens and walks away from. It is remembered
        instead, and the first thing it says both publishes it and proves whose
        it is, because the only client that can be in it is the one that opened
        it.
        """
        if self.is_closing(summary.thread_id):
            # A thread the device is closing (A39) is nobody's to describe yet.
            return
        self._used[summary.thread_id] = summary.updated_at
        entry = self.hub.entries.get(summary.thread_id)
        if entry is None:
            if summary.thread_id in self._quiet:
                self._quiet.remove(summary.thread_id)
                terminal = True
            elif threads.is_empty(summary) and not spoke:
                self._quiet.append(summary.thread_id)
                return
            entry = self.hub.register_mirrored(
                Session(
                    session_id=summary.thread_id,
                    device_id=self.hub.device_id,
                    agent="codex",
                    cwd=summary.cwd,
                    title=summary.title,
                    state="idle",
                    origin="terminal",
                    control="none",
                    model=summary.model,
                    effort=summary.effort,
                    created_at=summary.created_at,
                    updated_at=summary.updated_at,
                )
            )
            entry.channel.start()
        if terminal:
            # Amendment A15: a TUI is sitting in this thread, so it is live again.
            await entry.channel.revive()
        if summary.name:
            await titles.from_agent(entry.channel, summary.name)
        else:
            await titles.from_prompt(entry.channel, summary.title)
        if loaded:
            self._loaded.add(summary.thread_id)
            await self._attach(entry)
        else:
            self._loaded.discard(summary.thread_id)
        if terminal and isinstance(entry.runner, CodexDaemonSession):
            entry.runner.claim_terminal()
        await self.publish_control(entry)

    async def _attach(self, entry: SessionEntry) -> None:
        """Resume a loaded thread so its stream reaches the apps.

        A thread the terminal has just created is loaded but has no rollout
        until its first turn is persisted, and `thread/resume` refuses it until
        then; the session is already `shared` and the resume is retried when the
        next notification or scan says the thread is still there.
        """
        thread_id = entry.session.session_id
        if entry.runner is not None or entry.session.archived or thread_id in self._attaching:
            return
        self._attaching.add(thread_id)
        try:
            runner = self.session_for(entry, resume=thread_id)
            # Codex replays what is pending on a thread the moment it is resumed,
            # and a request finds its session through the entry: the runner goes
            # there first, or a terminal's open approval is replayed to nobody.
            entry.runner = runner
            await runner.start()
        except RcError as exc:
            entry.runner = None
            log.warning("could not attach to a codex thread", error=exc.message[:120])
            return
        finally:
            self._attaching.discard(thread_id)
        if runner.subscribed:
            await runner.publish_settings()

    def session_for(self, entry: SessionEntry, resume: str | None) -> CodexDaemonSession:
        """Build the runner for one session; the hub owns when it is started."""
        session = entry.session

        async def on_turn_end() -> None:
            self.hub.schedule_drain(entry)

        async def on_control_change() -> None:
            await self.publish_control(entry)

        async def on_thread_id(thread_id: str) -> None:
            self._known.add(thread_id)
            self._loaded.add(thread_id)
            await self.hub.rekey(entry, thread_id)

        async def on_closing(thread_id: str) -> None:
            self.closing_thread(thread_id)

        return CodexDaemonSession(
            entry.channel,
            self.client,
            cwd=session.cwd,
            catalog=self._catalog,
            model=session.model,
            permission_mode=session.permission_mode,
            effort=session.effort,
            speed=session.speed,
            thread_id=resume,
            thread_config=self._config,
            created_here=session.origin == "remote",
            on_turn_end=on_turn_end,
            on_control_change=on_control_change,
            on_thread_id=on_thread_id,
            on_closing=on_closing,
        )

    def _claim_order(self, entry: SessionEntry) -> tuple[int, int, str]:
        """Which threads in one directory a terminal is most likely to be in.

        The thread that already had the claim keeps it, so a terminal does not
        hop between threads while it sits there; the rest are ranked by how
        recently the daemon saw them used, which is the only thing separating
        the thread somebody is typing in from the ones they finished with last
        week.
        """
        thread_id = entry.session.session_id
        runner = entry.runner
        claimed = isinstance(runner, CodexDaemonSession) and runner.terminal_holds
        return (0 if claimed else 1, -self._used.get(thread_id, 0), thread_id)

    def _created_here(self, entry: SessionEntry) -> bool:
        """Whether this device started the thread, whoever is in it now."""
        runner = entry.runner
        return entry.session.origin == "remote" or (
            isinstance(runner, CodexDaemonSession) and runner.created_here
        )

    async def publish_control(self, entry: SessionEntry) -> None:
        """Apply the A11 table and announce a change the way every other one travels.

        A live subscription is proof the daemon has the thread, as speaking is:
        a session reopened after a close (A39) resumed the thread itself, and
        the index does not hear of it again until the next scan.
        """
        runner = entry.runner if isinstance(entry.runner, CodexDaemonSession) else None
        loaded = entry.session.session_id in self._loaded or (
            runner is not None and runner.subscribed
        )
        origin, control = threads.resolve(
            self._created_here(entry),
            loaded=loaded,
            terminal_holds=runner is not None and runner.terminal_holds,
            local_turn=runner is not None and runner.local_turn,
        )
        changed = entry.session.origin != origin or entry.session.control != control
        entry.session.origin = origin  # type: ignore[assignment]
        entry.session.control = control  # type: ignore[assignment]
        if changed:
            await entry.channel.emit("meta", control=control)
            if entry.session.state == "readonly":
                await entry.channel.set_state("idle")
            await entry.channel.publish_summary()

    # ---------------------------------------------------------- dispatching

    def _session(self, thread_id: str) -> CodexDaemonSession | None:
        entry = self.hub.entries.get(thread_id)
        if entry is None or not isinstance(entry.runner, CodexDaemonSession):
            return None
        return entry.runner

    async def _notification(self, method: str, params: dict[str, Any]) -> None:
        if method == "remoteControl/status/changed":
            log.info("codex remote control status", status=str(params.get("status") or ""))
            return
        if method == "thread/started":
            await self._thread_started(params)
            return
        thread_id = str(params.get("threadId") or "")
        if not thread_id:
            return
        parent_id = self._children.parent(thread_id)
        if parent_id is not None:
            await self._child_spoke(parent_id, thread_id, method, params)
            return
        if method == "thread/closed":
            await self._thread_closed(thread_id)
            return
        if method == "thread/archived":
            await self._thread_archived(thread_id)
            return
        if method == "thread/name/updated":
            await self._thread_named(thread_id, str(params.get("threadName") or ""))
            return
        if method == "serverRequest/resolved":
            session = self._session(thread_id)
            if session is not None:
                await session.resolved_elsewhere(params.get("requestId"))
            return
        session = self._session(thread_id)
        if session is None:
            # A thread we are not following just spoke: take it, then let it
            # speak, so the word that made it a session is not the one lost.
            await self._retry_attach(thread_id)
            session = self._session(thread_id)
            if session is None:
                return
        await session.notification(method, params)

    async def _retry_attach(self, thread_id: str) -> None:
        """A thread we have no runner for just spoke; take one now.

        A thread with no session at all is one that was empty when we met it,
        and this is the moment it stops being empty. A thread we do hold is
        alive because it spoke, whatever the archive still says: a session the
        device archived and a terminal then resumed leaves the Archive on the
        thread's first word, and the client that said it is not this one, so
        the terminal holds the thread (A15, design § "A session that speaks is
        alive"). Speaking is also proof the daemon has the thread loaded, which
        the index may still be a scan behind on.
        """
        if self.is_closing(thread_id):
            # The device is closing this thread on the person's word (A39): its
            # last words are not a reason to take the session out of the Archive.
            return
        entry = self.hub.entries.get(thread_id)
        if entry is None:
            if thread_id not in self._loaded:
                return
            async with self._lock:
                if thread_id not in self.hub.entries:
                    await self._adopt_by_id(thread_id, spoke=True)
            return
        self._loaded.add(thread_id)
        await entry.channel.revive()
        await self._attach(entry)
        if isinstance(entry.runner, CodexDaemonSession):
            entry.runner.claim_terminal()
        await self.publish_control(entry)

    async def _child_spoke(
        self, parent_id: str, child_id: str, method: str, params: dict[str, Any]
    ) -> None:
        """One of a session's subagents said something; only its parent shows it.

        A subagent's thread is never a session (A18), so nothing of it reaches
        the apps but the one fact the parent's row is made of: whether work the
        session started is still running.
        """
        if method == "thread/closed":
            self._children.forget(child_id)
        session = self._session(parent_id)
        if session is None:
            return
        if method == "turn/started" or (method.startswith("item/") and method != "item/completed"):
            active = True
        elif method in {"turn/completed", "thread/closed"}:
            active = False
        elif method == "thread/status/changed":
            status = params.get("status")
            if not isinstance(status, dict) or status.get("type") not in {"active", "idle"}:
                return
            active = threads.is_active(status)
        else:
            return
        await session.child_activity(child_id, active)

    async def _thread_started(self, params: dict[str, Any]) -> None:
        """Another client opened a thread, which is the one it is now sitting in."""
        thread = params.get("thread")
        if not isinstance(thread, dict) or threads.is_ephemeral(thread):
            return
        if await self._adopt_child(thread):
            return
        summary = threads.ThreadSummary.parse(thread)
        if summary is None:
            return
        if not summary.ours:
            self._foreign.append(summary.thread_id)
            return
        if self._session(summary.thread_id) is not None:
            return
        async with self._lock:
            self._known.add(summary.thread_id)
            self._loaded.add(summary.thread_id)
            await self._adopt(summary, loaded=True, terminal=True)

    async def _thread_named(self, thread_id: str, name: str) -> None:
        """Codex named the thread from its conversation; the sidebars follow it.

        The name arrives for every subscriber, so a thread named in a terminal
        renames itself in the apps too. It never overrides a title the user set.
        """
        entry = self.hub.entries.get(thread_id)
        if entry is not None and name:
            await titles.from_agent(entry.channel, name)

    async def _thread_archived(self, thread_id: str) -> None:
        """A thread archived in Codex, by this device's close (A39) or in a terminal.

        Codex moves the rollout to `archived_sessions/` and takes the thread out
        of both `thread/loaded/list` and `thread/list`, so the index forgets it
        outright: a thread missing from the history page is otherwise read as
        one deleted in Codex, and the session would go with it. The session
        record stays where it is — closed here, it is the row in the Archive —
        and a message sent to it later unarchives the thread and resumes it.
        """
        closed_here = self.is_closing(thread_id)
        self._forget(thread_id)
        entry = self.hub.entries.get(thread_id)
        if entry is None:
            return
        if isinstance(entry.runner, CodexDaemonSession):
            await entry.runner.close()
            entry.runner = None
        if closed_here:
            # The close has already said what the session is: archived, stopped
            # and unowned (A39). Nothing here is news.
            return
        await self.publish_control(entry)

    def _forget(self, thread_id: str) -> None:
        """Drop one thread from the index, leaving its session alone."""
        self._known.discard(thread_id)
        self._loaded.discard(thread_id)
        self._used.pop(thread_id, None)
        if thread_id in self._quiet:
            self._quiet.remove(thread_id)

    async def _thread_closed(self, thread_id: str) -> None:
        self._loaded.discard(thread_id)
        if thread_id in self._quiet:
            self._quiet.remove(thread_id)
        closed_here = self._closing.pop(thread_id, None) is not None
        entry = self.hub.entries.get(thread_id)
        if entry is None:
            return
        if isinstance(entry.runner, CodexDaemonSession):
            await entry.runner.close()
            entry.runner = None
        if closed_here:
            # The close this answers has already said what the session is:
            # archived, stopped and unowned (A39). Nothing to publish.
            return
        await self.publish_control(entry)

    async def _request(self, request_id: Any, method: str, params: dict[str, Any]) -> Any:
        thread_id = str(params.get("threadId") or "")
        session = self._session(thread_id)
        if session is None:
            # Nobody here follows this thread, so whoever does can answer it, and
            # an error from this side would settle the request before they could.
            raise NoReply(f"no attached session for {method}")
        if _unattended(session) and not dialogs.presents(method):
            # Nobody else can answer what the apps cannot show, and a request
            # left open would stop the turn for good: an elicitation has a
            # decline that needs no person, and anything else is refused.
            if method == dialogs.ELICITATION_METHOD:
                return dialogs.elicitation_decline()
            raise RcError("unsupported", f"unhandled codex request {method}")
        return await session.server_request(request_id, method, params)

    async def _reconnected(self) -> None:
        """Re-resume everything we were following and backfill what we missed."""
        for entry in list(self.hub.entries.values()):
            runner = entry.runner
            if not isinstance(runner, CodexDaemonSession):
                continue
            try:
                await runner.resubscribe()
            except RcError as exc:
                log.warning("could not re-resume a codex thread", error=exc.message[:200])
        await self.refresh()
