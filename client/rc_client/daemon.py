"""The daemon: agent discovery, the gateway link, the session hub and mirroring."""

from __future__ import annotations

import asyncio
import contextlib
import platform
import signal
import socket
from typing import Any

from . import __version__, attachments
from . import config as config_module
from .agents import announced
from .agents.codex.daemon.service import CodexDaemonService
from .agents.codex.runtime import resolve_binary as resolve_codex
from .agents.grok.runtime import resolve_binary as resolve_grok
from .agents.grok.service import GrokLeaderService
from .agents.pi import paths as pi_paths
from .agents.pi.link import PiExtensionServer
from .agents.pi.service import PiExtensionService
from .agents.registry import AGENT_IDS, DetectContext, detect_all, detect_one
from .agents.startup import AgentStartup
from .build import as_digest, read_build
from .channel import paths as channel_paths
from .channel.mcp_config import write_mcp_config
from .channel.settings import write_settings
from .child_env import scrub_parent_secrets
from .config import Config
from .errors import RcError
from .fs import list_dirs, make_dir, merge_recents
from .gateway import GatewayLink, Handler
from .git import git_info
from .instance_lock import InstanceLock, lock_path
from .logging_setup import logger
from .looplag import watch_loop_lag
from .models import AgentInfo
from .registry import Registry
from .sessions.attach import AttachServer
from .sessions.busy import busy_sessions
from .sessions.hub import SessionHub
from .sessions.mirror import MirrorService
from .terminal import TerminalManager
from .update import log_tail, spawn_self_update

log = logger("rc_client.daemon")

AGENT_REFRESH_INTERVAL = 900.0
# How long the first hello of a run waits for the first scan to say which terminals still hold
# their sessions. The scan runs while the link connects, so this is the most it can cost.
FIRST_SCAN_WAIT = 3.0
# How long a request that acts through an agent waits for that agent to start after a restart,
# inside the 20 s the native apps give a request. Past it the request is refused and never run.
AGENT_START_WAIT = 15.0
# The requests that act through an agent. Each waits for its own agent to start rather than fail;
# nothing else waits, a history read least of all.
AGENT_REQUESTS = frozenset(
    {
        "session.create",
        "session.send",
        "session.stop",
        "session.set",
        "session.commands",
        "session.command",
        "session.takeover",
        "session.archive",
        "session.delete",
    }
)
FROM_SOURCE = "this client was installed from source; update it on the host"


class Daemon:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.instance_lock = InstanceLock(lock_path())
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
        self._greeted = False
        # What the last hello or `agents.updated` of this run said the agents are, once one has.
        self._said: list[dict[str, Any]] | None = None
        # What the last run detected, said by a hello that comes before this run's detection.
        self._last_detected: list[dict[str, Any]] = []
        # Every agent starts on its own once the link is up, and a request waits for its own alone.
        self._startup = AgentStartup(
            detect=self._detect_one,
            found=self._found,
            changed=self._agents_changed,
            before={
                "codex": lambda: self.codex.start(resolve_codex()),
                "grok": lambda: self.grok.ensure(resolve_grok()),
            },
            # Withdrawing the Codex sessions that are not the device's comes last (A59).
            after={"codex": lambda: self.codex.prune_foreign()},
        )
        self._refresh_task: asyncio.Task[None] | None = None
        self._lag_task: asyncio.Task[None] | None = None
        self._update_task: asyncio.Task[None] | None = None
        self._sweep_task: asyncio.Task[None] | None = None

    # ------------------------------------------------------------- lifecycle

    async def run(self) -> None:
        # Before anything binds, unlinks or dials: a second daemon on this home
        # would delete the first one's sockets, as it starts and as it exits.
        await self._claim_home()
        scrub_parent_secrets()
        self.hub.load()
        # Amendment A35: the resumes the device owes, read back before anything
        # can publish a session, so a restart does not lose one.
        self.hub.resumes.load()
        self._last_detected = announced.load(self.registry)
        # The bridges and terminal proxies of the CLIs that outlived the last daemon have been
        # dialling back since it went.
        await self._prepare_attachment()
        # The link waits for none of the agents. Reaching the shared Codex daemon, joining the
        # Grok leader and asking every CLI what it offers took minutes on a loaded machine with a
        # long history, and the device is reachable without them: a request that needs one waits
        # for that one instead (`AGENT_REQUESTS`).
        self._startup.start()
        log.info("device daemon starting", device=self.config.device_id)
        # Before the link, so the first scan runs while it connects and the first hello can say
        # which terminals still hold their sessions.
        self.mirror.start()
        self.link.start()
        self.hub.resumes.start()
        # A58: the processes of the sessions the device holds stop after 30 minutes unused.
        self.hub.idle.start()
        self._refresh_task = asyncio.create_task(self._refresh_loop())
        self._lag_task = asyncio.create_task(watch_loop_lag(), name="loop-lag")
        self._sweep_task = asyncio.create_task(self._sweep_loop(), name="attachment-sweep")
        try:
            await self._wait_for_stop()
        finally:
            await self.shutdown()

    async def _claim_home(self) -> None:
        """Hold this home's single-instance lock, or refuse to run beside the daemon that does.

        A filesystem that cannot lock is no reason to stay down: the daemon then
        runs as it did before there was a lock, and says so.
        """
        try:
            await self.instance_lock.acquire()
        except OSError as exc:
            log.warning("single-instance lock unavailable", error=str(exc))

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

    async def _detect_one(self, agent: str) -> AgentInfo | None:
        return await detect_one(agent, self._detect_context())

    def _found(self, agent: str, info: AgentInfo | None) -> None:
        """One agent has started: the hub acts through what it said from now on."""
        known = {each.agent: each for each in self.agents}
        if info is None:
            known.pop(agent, None)
        else:
            known[agent] = info
        self.agents = [known[each] for each in AGENT_IDS if each in known]

    async def _detected(self, agents: list[AgentInfo]) -> None:
        """A detection of every agent: keep it, and say it when it is news."""
        self.agents = agents
        await self._agents_changed()

    async def _agents_changed(self) -> None:
        """Say the agents when they are news, and keep them once every one has started."""
        listed = self._agent_list()
        if self._startup.done:
            announced.save(self.registry, listed)
            log.info(
                "agents detected",
                agents=",".join(each.agent for each in self.agents if each.available),
            )
        if self._said is not None and listed != self._said:
            self._said = listed
            await self._publish({"type": "agents.updated", "agents": listed})

    def _agent_list(self) -> list[dict[str, Any]]:
        """The agents a hello lists: what this run has found, and the last run's for the rest."""
        listed = {each.agent: each.to_dict() for each in self.agents}
        for item in self._last_detected:
            agent = item.get("agent")
            if isinstance(agent, str) and agent not in listed and not self._startup.started(agent):
                listed[agent] = item
        return [listed[each] for each in AGENT_IDS if each in listed]

    async def _first_hello_wait(self) -> None:
        """Give the first scan, and a detection no earlier run has made, `FIRST_SCAN_WAIT`.

        Both run while the link connects. A device with a list from its last run says that one
        rather than wait (`_agent_list`); one without would tell the gateway it has no agents.
        """
        scan = self.mirror.wait_settled(FIRST_SCAN_WAIT)
        if self._last_detected:
            settled = await scan
        else:
            settled, _ = await asyncio.gather(scan, self._startup.wait_all(FIRST_SCAN_WAIT))
        if not settled:
            log.warning(
                "first hello sent before the first terminal scan settled", waited=FIRST_SCAN_WAIT
            )

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
        # Before the channel socket goes: a bridge that closes from here on is this daemon's own
        # doing, and its CLI is still there.
        self.hub.begin_close()
        await self._startup.stop()
        for name in ("_refresh_task", "_lag_task", "_update_task", "_sweep_task"):
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
        # Last: a daemon waiting for this home binds the moment it is let go, and
        # the sockets above must be gone by then or their unlink would take its own.
        self.instance_lock.release()

    async def _refresh_loop(self) -> None:
        while True:
            await asyncio.sleep(AGENT_REFRESH_INTERVAL)
            try:
                agents = await detect_all(self._detect_context())
            except Exception:
                log.exception("agent re-detection failed")
                continue
            if [info.to_dict() for info in agents] != [info.to_dict() for info in self.agents]:
                await self._detected(agents)

    async def _sweep_loop(self) -> None:
        """Keep the attachments within their age and size, from start-up on (A49).

        A failure is reported by type alone: what a file error says names the
        file, and the file is something the person sent.
        """
        while True:
            try:
                await asyncio.to_thread(attachments.sweep)
            except Exception as exc:
                log.warning("attachment sweep failed", error=type(exc).__name__)
            await asyncio.sleep(attachments.SWEEP_INTERVAL)

    async def _codex_mode_changed(self) -> None:
        """The shared daemon appeared: apps learn about it through `agents.updated`."""
        await self._detected(await detect_all(self._detect_context()))

    async def _publish(self, frame: dict[str, Any]) -> None:
        await self.link.send(frame)

    async def _on_ready(self) -> None:
        # The scan is what the gateway backfills from, so a failure to say what is queued must not
        # cost it, and the two things to say do not depend on each other.
        for announce in (self.hub.clear_stale_queues, self.hub.republish_queues):
            try:
                await announce()
            except Exception:
                log.exception("telling the gateway about the Up next queues failed")
        await self.mirror.scan_once()

    async def _hello(self) -> dict[str, Any]:
        # Once: a reconnect has the answer already, or has waited for it.
        if not self._greeted:
            self._greeted = True
            await self._first_hello_wait()
        self._said = self._agent_list()
        return {
            "client_build": read_build(),
            "name": self.config.name or socket.gethostname(),
            "platform": "macos" if platform.system() == "Darwin" else "linux",
            "hostname": socket.gethostname(),
            "arch": "arm64" if platform.machine() in {"arm64", "aarch64"} else "x86_64",
            "agents": self._said,
            "sessions": self.hub.snapshot(),
            "terminal": self.config.terminal.enabled,
        }

    # -------------------------------------------------------------- handlers

    def _handlers(self) -> dict[str, Any]:
        handlers = self._requests()
        for kind in AGENT_REQUESTS:
            handlers[kind] = self._after_agent(kind, handlers[kind])
        return handlers

    def _after_agent(self, kind: str, handler: Handler) -> Handler:
        """The handler, once the agent the request acts through has started.

        Without it a request would find no agent to act through, or Codex's fallback path for a
        thread the shared daemon may be serving. One that waits `AGENT_START_WAIT` in vain is
        refused and never run, so an app that has given up on it never sees it carried out later.
        """

        async def handle(frame: dict[str, Any]) -> dict[str, Any]:
            agent = self._acting_agent(kind, frame)
            if agent is not None and not await self._startup.wait(agent, AGENT_START_WAIT):
                raise RcError(
                    "agent_unavailable",
                    f"{agent} is still starting on this device; try again in a moment",
                )
            return await handler(frame)

        return handle

    def _acting_agent(self, kind: str, frame: dict[str, Any]) -> str | None:
        """The agent a request acts through: the one it names, or its session's."""
        if kind == "session.create":
            agent = frame.get("agent")
            return agent if isinstance(agent, str) else None
        entry = self.hub.entries.get(str(frame.get("session_id") or ""))
        return entry.session.agent if entry is not None else None

    def _requests(self) -> dict[str, Any]:
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
            grok_billing=self.grok.billing if limits else None,
            proxy=self.config.proxy,
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
        busy = busy_sessions(self.hub.entries.values())
        if busy:
            plural = "s are" if busy > 1 else " is"
            raise RcError("conflict", f"{busy} session{plural} running")
        if self.terminals.count:
            # A38: a shell ends with the daemon, and nothing in the gateway's index shows one.
            plural = "s are" if self.terminals.count > 1 else " is"
            raise RcError("conflict", f"{self.terminals.count} terminal{plural} open")
        self._update_task = asyncio.create_task(self._update(requested), name="self-update")
        return {"accepted": True, "from": current}

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
