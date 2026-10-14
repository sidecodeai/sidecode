"""The daemon: agent discovery, the gateway link, the session hub and mirroring."""

from __future__ import annotations

import asyncio
import contextlib
import platform
import signal
import socket
from typing import Any

from . import __version__
from . import config as config_module
from .agents.codex.daemon.service import CodexDaemonService
from .agents.codex.runtime import resolve_binary as resolve_codex
from .agents.grok.runtime import resolve_binary as resolve_grok
from .agents.grok.service import GrokLeaderService
from .agents.pi import paths as pi_paths
from .agents.pi.link import PiExtensionServer
from .agents.pi.service import PiExtensionService
from .agents.registry import DetectContext, detect_all
from .build import as_digest, read_build
from .channel import paths as channel_paths
from .channel.mcp_config import write_mcp_config
from .channel.settings import write_settings
from .child_env import scrub_parent_secrets
from .config import Config
from .errors import RcError
from .fs import list_dirs, make_dir, merge_recents
from .gateway import GatewayLink
from .git import git_info
from .logging_setup import logger
from .looplag import watch_loop_lag
from .models import AgentInfo
from .registry import Registry
from .sessions.attach import AttachServer
from .sessions.hub import SessionHub
from .sessions.mirror import MirrorService
from .terminal import TerminalManager
from .update import log_tail, spawn_self_update

log = logger("rc_client.daemon")

AGENT_REFRESH_INTERVAL = 900.0
# A session in one of these states is doing work an update would throw away.
BUSY_STATES = frozenset({"starting", "running", "needs_approval", "needs_input"})
FROM_SOURCE = "this client was installed from source; update it on the host"


class Daemon:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.registry = Registry(config_module.database_path())
        self.agents: list[AgentInfo] = []
        self.hub = SessionHub(self.registry, self._publish, config.device_id, lambda: self.agents)
        self.codex = CodexDaemonService(self.hub, __version__, self._codex_mode_changed)
        self.hub.codex_daemon = self.codex
        self.grok = GrokLeaderService(self.hub)
        self.hub.grok_leader = self.grok
        self.mirror = MirrorService(
            self.hub, config.mirror, codex_daemon=self.codex, grok_leader=self.grok
        )
        self.attach = AttachServer(channel_paths.socket_path(), self.hub)
        # A40: the terminals the shim started, which the hub types into.
        self.hub.ptys = self.attach.ptys
        self.pi = PiExtensionService(self.hub)
        self.hub.pi_extensions = self.pi
        self.pi_socket = PiExtensionServer(pi_paths.socket_path(), self.pi)
        self.terminals = TerminalManager(self._publish, enabled=config.terminal.enabled)
        self.link = GatewayLink(
            config.device_ws_url,
            config.device_token,
            hello=self._hello,
            handlers=self._handlers(),
            signals={"preferences": self._preferences},
            on_ready=self._on_ready,
            proxy=config.proxy,
        )
        self.hub.link_up = lambda: self.link.connected
        self._refresh_task: asyncio.Task[None] | None = None
        self._lag_task: asyncio.Task[None] | None = None
        self._update_task: asyncio.Task[None] | None = None

    # ------------------------------------------------------------- lifecycle

    async def run(self) -> None:
        scrub_parent_secrets()
        self.hub.load()
        # Amendment A35: the resumes the device owes, read back before anything
        # can publish a session, so a restart does not lose one.
        self.hub.resumes.load()
        await self.codex.start(resolve_codex())
        # A leader the person's configuration asks for, started here when no TUI
        # has started one yet (A28). Failing to reach it is not fatal: Grok
        # sessions then run on private children and terminal ones are mirrored.
        await self.grok.ensure(resolve_grok())
        self.agents = await detect_all(self._detect_context())
        log.info(
            "device daemon starting",
            device=self.config.device_id,
            agents=",".join(info.agent for info in self.agents if info.available),
        )
        await self._prepare_attachment()
        self.link.start()
        self.mirror.start()
        self.hub.resumes.start()
        self._refresh_task = asyncio.create_task(self._refresh_loop())
        self._lag_task = asyncio.create_task(watch_loop_lag(), name="loop-lag")
        try:
            await self._wait_for_stop()
        finally:
            await self.shutdown()

    async def _prepare_attachment(self) -> None:
        """Publish the files the shim points at and open the channel socket.

        None of it is fatal: without them the device simply cannot attach to
        terminal sessions, and everything else keeps working.
        """
        try:
            write_mcp_config()
            write_settings()
            await self.attach.start()
        except OSError as exc:
            log.warning("terminal attachment unavailable", error=str(exc))
        try:
            await self.pi_socket.start()
        except OSError as exc:
            log.warning("pi attachment unavailable", error=str(exc))

    async def _wait_for_stop(self) -> None:
        """Block until the process is asked to stop, so shutdown actually runs."""
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        installed: list[signal.Signals] = []
        for signal_number in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(signal_number, stop.set)
            except (NotImplementedError, RuntimeError, ValueError):
                continue
            installed.append(signal_number)
        try:
            await stop.wait()
        finally:
            for signal_number in installed:
                with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
                    loop.remove_signal_handler(signal_number)

    async def shutdown(self) -> None:
        for name in ("_refresh_task", "_lag_task", "_update_task"):
            task: asyncio.Task[None] | None = getattr(self, name)
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
                setattr(self, name, None)
        await self.hub.resumes.stop()
        await self.mirror.stop()
        await self.codex.stop()
        await self.grok.stop()
        await self.attach.stop()
        await self.pi_socket.stop()
        await self.terminals.stop()
        await self.hub.close()
        await self.link.stop()
        self.registry.close()

    async def _refresh_loop(self) -> None:
        while True:
            await asyncio.sleep(AGENT_REFRESH_INTERVAL)
            try:
                agents = await detect_all(self._detect_context())
            except Exception:
                log.exception("agent re-detection failed")
                continue
            if [info.to_dict() for info in agents] != [info.to_dict() for info in self.agents]:
                self.agents = agents
                await self._publish(
                    {"type": "agents.updated", "agents": [info.to_dict() for info in agents]}
                )

    async def _codex_mode_changed(self) -> None:
        """The shared daemon appeared: apps learn about it through `agents.updated`."""
        self.agents = await detect_all(self._detect_context())
        await self._publish(
            {"type": "agents.updated", "agents": [info.to_dict() for info in self.agents]}
        )

    async def _publish(self, frame: dict[str, Any]) -> None:
        await self.link.send(frame)

    async def _on_ready(self) -> None:
        await self.mirror.scan_once()

    async def _hello(self) -> dict[str, Any]:
        return {
            "client_build": read_build(),
            "name": self.config.name or socket.gethostname(),
            "platform": "macos" if platform.system() == "Darwin" else "linux",
            "hostname": socket.gethostname(),
            "arch": "arm64" if platform.machine() in {"arm64", "aarch64"} else "x86_64",
            "agents": [info.to_dict() for info in self.agents],
            "sessions": self.hub.snapshot(),
            "terminal": self.config.terminal.enabled,
        }

    # -------------------------------------------------------------- handlers

    def _handlers(self) -> dict[str, Any]:
        return {
            "session.create": self.hub.create,
            "session.send": self.hub.send,
            "session.stop": self.hub.stop,
            "session.approve": self.hub.approve,
            "session.answer": self.hub.answer,
            "session.set": self.hub.set_options,
            "session.history": self.hub.history,
            "session.block": self.hub.block,
            "session.commands": self.hub.commands,
            "session.command": self.hub.command,
            "session.queue_remove": self.hub.queue_remove,
            "session.resume_set": self.hub.resumes.set_request,
            "session.resume_cancel": self.hub.resumes.cancel_request,
            "session.takeover": self.hub.takeover,
            "session.archive": self.hub.archive,
            "session.delete": self.hub.delete,
            "terminal.open": self.terminals.open,
            "terminal.input": self.terminals.input,
            "terminal.resize": self.terminals.resize,
            "terminal.attach": self.terminals.attach,
            "terminal.close": self.terminals.close,
            "terminal.detach": self.terminals.detach,
            "device.dirs": self._device_dirs,
            "device.mkdir": self._device_mkdir,
            "device.git": self._device_git,
            "device.agents": self._device_agents,
            "device.update": self._device_update,
        }

    async def _preferences(self, frame: dict[str, Any]) -> None:
        """The account's switches, after `hello_ack` and on every change (A35)."""
        preferences = frame.get("preferences")
        preferences = preferences if isinstance(preferences, dict) else {}
        await self.hub.resumes.set_enabled(bool(preferences.get("resume_after_limit")))

    async def _device_dirs(self, params: dict[str, Any]) -> dict[str, Any]:
        recents = merge_recents(self._session_recents())
        return await asyncio.to_thread(list_dirs, params.get("path"), recents)

    async def _device_mkdir(self, params: dict[str, Any]) -> dict[str, Any]:
        """A37: make one folder where a session will work, and list it."""
        path = params.get("path")
        name = params.get("name")
        if not isinstance(path, str) or not isinstance(name, str):
            raise RcError("bad_request", "path and name are required")
        recents = merge_recents(self._session_recents())
        return await asyncio.to_thread(make_dir, path, name, recents)

    def _session_recents(self) -> list[tuple[str, int]]:
        return [
            (entry.session.cwd, entry.session.updated_at)
            for entry in self.hub.entries.values()
            if entry.session.cwd
        ]

    async def _device_git(self, params: dict[str, Any]) -> dict[str, Any]:
        path = str(params.get("path") or "")
        if not path:
            raise RcError("bad_request", "path is required")
        return await git_info(path)

    async def _device_agents(self, params: dict[str, Any]) -> dict[str, Any]:
        agents = await detect_all(self._detect_context(limits=True))
        # This reply is the only frame that carries quota (A33). What the device
        # stores is what it publishes, so it keeps the accounts without their
        # windows: otherwise every reply would look like news to the refresh
        # loop and `agents.updated` would follow each one.
        self.agents = [info.without_limits() for info in agents]
        return {"agents": [info.to_dict() for info in agents]}

    def _detect_context(self, limits: bool = False) -> DetectContext:
        """What detection needs from the daemon, and quota only when asked."""
        ready = self.codex.ready
        return DetectContext(
            codex_daemon_ready=ready,
            limits=limits,
            codex_rate_limits=self.codex.rate_limits if limits and ready else None,
        )

    # ----------------------------------------------------------------- update

    async def _device_update(self, params: dict[str, Any]) -> dict[str, Any]:
        """Accept an app's request to install the build the gateway serves (A22)."""
        requested = as_digest(str(params.get("build") or ""))
        if requested is None:
            raise RcError("bad_request", "build must be a SHA-256 hex digest")
        current = read_build()
        if current is None:
            raise RcError("unsupported", FROM_SOURCE)
        if current == requested:
            raise RcError("conflict", "already on this build")
        if self._update_task is not None and not self._update_task.done():
            raise RcError("conflict", "an update is already running")
        busy = self._busy_sessions()
        if busy:
            plural = "s are" if busy > 1 else " is"
            raise RcError("conflict", f"{busy} session{plural} running")
        self._update_task = asyncio.create_task(self._update(requested), name="self-update")
        return {"accepted": True, "from": current}

    def _busy_sessions(self) -> int:
        return sum(
            1
            for entry in self.hub.entries.values()
            if entry.runner is not None and entry.session.state in BUSY_STATES
        )

    async def _update(self, build: str) -> None:
        """Watch the detached updater, so a failure is reported before the restart."""
        try:
            process = await spawn_self_update(build)
        except (OSError, RcError) as exc:
            log.warning("the updater could not start", error=type(exc).__name__)
            await self._publish({"type": "update.failed", "message": "the updater could not start"})
            return
        if await process.wait() == 0:
            return
        await self._publish({"type": "update.failed", "message": log_tail()})
