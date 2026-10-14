"""The device's view of the Grok leader: one connection, many joined sessions.

Grok's own registry (`~/.grok/active_sessions.json`) is the only signal that a
person is sitting in a session: the leader says nothing when a TUI exits and
offers no way to ask who is attached. So every scan reads the registry, and a
live entry the leader also reports loaded is a leader-mode TUI — the device
joins that session with `session/load` and publishes it as `shared` (A28). A
live entry the leader does not have is a `grok` running its own agent, with
`use_leader` off or a sandbox profile on, and the update-log mirror keeps it.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import TYPE_CHECKING, Any

from ...errors import RcError
from ...logging_setup import logger
from ...models import Session, now_ms
from ...sessions import titles
from . import catalog as catalogue
from . import control, leader, runtime
from . import sessions as grok_sessions
from .adapter import GrokRunner

if TYPE_CHECKING:  # pragma: no cover - imported for types only
    from ...sessions.hub import SessionEntry, SessionHub

log = logger("rc_client.grok.service")

# How long to leave a drifted leader alone between restart attempts.
DRIFT_INTERVAL = 900.0
KILL_TIMEOUT = 15.0


class GrokLeaderService:
    """Maps the leader's sessions onto hub sessions, on the mirror's scan."""

    def __init__(self, hub: SessionHub) -> None:
        self.hub = hub
        self._client: leader.LeaderClient | None = None
        # Sessions this device holds in the leader, which the mirror leaves alone.
        self._known: set[str] = set()
        # Sessions a live TUI has registered, as of the last scan.
        self._registered: set[str] = set()
        # Titles the leader has announced, kept for sessions not yet adopted.
        self._titles: dict[str, str] = {}
        self._binary: str | None = None
        self._drifted_at = 0.0
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------- accessors

    @property
    def ready(self) -> bool:
        return self._client is not None and self._client.connected

    def knows(self, session_id: str) -> bool:
        """Whether the leader holds this session, so the mirror leaves it alone."""
        return session_id in self._known

    # ------------------------------------------------------------- lifecycle

    async def ensure(self, binary: str | None = None) -> bool:
        """Connect to the leader when the person's configuration asks for it.

        Connecting starts a leader if none is running, which is what the next
        `grok` would do anyway; without `[cli] use_leader` the device stays on
        the private-child path and every terminal session stays mirrored.
        """
        if binary:
            self._binary = binary
        if self.ready:
            return True
        if not await asyncio.to_thread(leader.config_ready):
            return False
        path = self._binary or await asyncio.to_thread(runtime.resolve_binary)
        if not path:
            return False
        self._binary = path
        client = self._client
        if client is None:
            client = leader.LeaderClient(
                path,
                on_unknown=self._unknown,
                on_sessions_changed=self._sessions_changed,
            )
            self._client = client
        return await client.connect()

    async def stop(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            await client.close()

    # ------------------------------------------------------------------ scan

    async def tick(self, binary: str | None) -> None:
        """Reconcile the registry, the leader and the hub. Runs on every scan."""
        if binary:
            self._binary = binary
        entries, known = await asyncio.to_thread(grok_sessions.active_entries)
        live: dict[str, grok_sessions.RegisteredSession] = {}
        if known:
            live = {entry.session_id: entry for entry in entries if entry.live}
        async with self._lock:
            if self._client is not None and not self._client.connected:
                await self._reconnect()
            self._registered = set(live)
            await self._adopt(live)
            await self._refresh_control()
            await self._check_drift(live)

    async def _adopt(self, live: dict[str, grok_sessions.RegisteredSession]) -> None:
        """Take every registered session the leader turns out to be holding."""
        for session_id, registered in live.items():
            if session_id in self._known:
                continue
            if not await self.ensure():
                return
            client = self._client
            if client is None:  # pragma: no cover - narrowing
                return
            info = await client.session_info(session_id)
            if not info:
                # A `grok` running its own agent; the mirror keeps reading its log.
                continue
            await self._take(session_id, registered)

    async def _take(self, session_id: str, registered: grok_sessions.RegisteredSession) -> None:
        """Adopt one leader-mode terminal session and join it."""
        entry = self.hub.entries.get(session_id)
        if entry is None:
            stamp = now_ms()
            entry = self.hub.register_mirrored(
                Session(
                    session_id=session_id,
                    device_id=self.hub.device_id,
                    agent="grok",
                    cwd=registered.cwd,
                    title="",
                    state="idle",
                    origin="terminal",
                    control="shared",
                    created_at=stamp,
                    updated_at=stamp,
                )
            )
            entry.channel.start()
        if registered.cwd and not entry.session.cwd:
            entry.session.cwd = registered.cwd
        entry.holder_pid = registered.pid or None
        await entry.channel.revive()
        if entry.runner is None:
            # The runner goes on the entry first: `session/load` replays before
            # it answers, and a callback that replay fires must not read the
            # session as unattached and flap its control back to `terminal`.
            runner = self.runner_for(entry, resume=session_id)
            entry.runner = runner
            self._known.add(session_id)
            try:
                await runner.start()
            except RcError as exc:
                entry.runner = None
                self._known.discard(session_id)
                log.warning("could not join a grok session", error=exc.message[:200])
                return
        self._known.add(session_id)
        await self._name(entry)
        await self.publish_control(entry)

    async def _name(self, entry: SessionEntry) -> None:
        """The title the leader knows, else the one Grok wrote for itself.

        Three sources in order: what `_x.ai/sessions/changed` already announced,
        what the leader's own index says about this directory, and the summary
        Grok writes beside the session on disk.
        """
        session_id = entry.session.session_id
        title = self._titles.get(session_id)
        client = self._client
        if not title and client is not None and client.connected:
            listed = await client.titles(entry.session.cwd)
            self._titles.update(listed)
            title = listed.get(session_id)
        if not title:
            title = await asyncio.to_thread(grok_sessions.title_for, session_id, entry.session.cwd)
        if title:
            await titles.from_agent(entry.channel, title)

    async def _refresh_control(self) -> None:
        for session_id in sorted(self._known):
            entry = self.hub.entries.get(session_id)
            if entry is None:
                self._known.discard(session_id)
                continue
            await self.publish_control(entry)

    # --------------------------------------------------------------- control

    async def publish_control(self, entry: SessionEntry) -> None:
        """Apply the A28 table and announce a change the way every other one travels."""
        runner = entry.runner if isinstance(entry.runner, GrokRunner) else None
        origin, new_control = control.resolve(
            entry.session.origin == "remote",
            registered=entry.session.session_id in self._registered,
            attached=runner is not None and runner.attached,
            local_turn=runner is not None and runner.local_turn,
        )
        changed = entry.session.origin != origin or entry.session.control != new_control
        entry.session.origin = origin  # type: ignore[assignment]
        entry.session.control = new_control  # type: ignore[assignment]
        if changed:
            await entry.channel.emit("meta", control=new_control)
            if entry.session.state == "readonly":
                await entry.channel.set_state("idle")
            await entry.channel.publish_summary()

    # --------------------------------------------------------------- runners

    def runner_for(self, entry: SessionEntry, resume: str | None) -> GrokRunner:
        """Build the runner for one session on the leader; the caller starts it."""
        session = entry.session

        async def on_turn_end() -> None:
            await self.hub.drain_queue(entry)
            await self.publish_control(entry)

        async def on_session_id(session_id: str) -> None:
            self._known.add(session_id)
            await self.hub.rekey(entry, session_id)

        client = self._client
        if client is None:  # pragma: no cover - the caller checked `ensure`
            raise RcError("agent_unavailable", "the grok leader is not connected")
        return GrokRunner(
            entry.channel,
            binary=self._binary or "",
            cwd=session.cwd,
            catalog=catalogue.load(),
            model=session.model,
            permission_mode=session.permission_mode,
            effort=session.effort,
            resume=resume,
            leader=client,
            on_turn_end=on_turn_end,
            on_session_id=on_session_id,
        )

    # -------------------------------------------------------------- the link

    async def _reconnect(self) -> None:
        """The leader child died: join again and re-load everything we held."""
        client = self._client
        if client is None or not await client.connect():
            return
        for session_id in sorted(self._known):
            entry = self.hub.entries.get(session_id)
            runner = entry.runner if entry is not None else None
            if not isinstance(runner, GrokRunner) or not runner.attached:
                continue
            try:
                await runner.resubscribe()
            except RcError as exc:
                log.warning("could not re-join a grok session", error=exc.message[:200])

    async def _unknown(self, session_id: str, method: str, params: dict[str, Any]) -> None:
        """A session nothing here holds just spoke.

        Nothing to do: the registry scan decides what this device may join, and
        a session the leader is running for another client is not its business.
        """
        log.debug("grok leader spoke about a session we do not hold", method=method)

    async def _sessions_changed(self, params: dict[str, Any]) -> None:
        """The leader's own session index changed; it carries the titles."""
        for row in params.get("upserted") or []:
            if not isinstance(row, dict):
                continue
            session_id = row.get("sessionId")
            title = row.get("title")
            if not isinstance(session_id, str) or not isinstance(title, str) or not title.strip():
                continue
            self._titles[session_id] = title.strip()
            entry = self.hub.entries.get(session_id)
            if entry is not None:
                await titles.from_agent(entry.channel, title.strip())

    # ----------------------------------------------------------------- drift

    async def _check_drift(self, live: dict[str, grok_sessions.RegisteredSession]) -> None:
        """Restart a leader older than the Grok on disk, when nobody is in it.

        The leader keeps serving the build it started with, so an update leaves
        every client on the old one. Restarting drops every session it holds, so
        it waits for an empty registry and for every device turn to finish.
        """
        client = self._client
        binary = self._binary
        if client is None or not client.connected or not binary or live:
            return
        if any(self._driving(session_id) for session_id in self._known):
            return
        installed = await runtime.probe_version(binary)
        if not installed or client.version is None or installed == client.version:
            return
        now = time.monotonic()
        if self._drifted_at and now - self._drifted_at < DRIFT_INTERVAL:
            return
        self._drifted_at = now
        log.info("restarting a drifted grok leader", leader=client.version, installed=installed)
        await self._kill_leader(binary)
        await client.close()
        await self._notice(f"restarted the Grok leader to pick up version {installed}")
        await self._reconnect()

    def _driving(self, session_id: str) -> bool:
        entry = self.hub.entries.get(session_id)
        runner = entry.runner if entry is not None else None
        return isinstance(runner, GrokRunner) and runner.busy

    async def _kill_leader(self, binary: str) -> None:
        """`agent leader kill` finds the socket in `GROK_HOME` and nothing else."""
        try:
            process = await asyncio.create_subprocess_exec(
                binary,
                "leader",
                "kill",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except OSError as exc:
            log.warning("could not run the grok leader kill", error=str(exc)[:200])
            return
        try:
            await asyncio.wait_for(process.wait(), timeout=KILL_TIMEOUT)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
                await process.wait()

    async def _notice(self, text: str) -> None:
        for session_id in sorted(self._known):
            entry = self.hub.entries.get(session_id)
            if entry is not None:
                await entry.channel.notice("info", text)
