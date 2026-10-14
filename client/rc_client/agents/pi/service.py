"""The device's view of every pi process that has loaded its extension.

A `hello` in `rpc` mode belongs to a child the hub already started: the link is
handed to that runner and carries nothing but approvals, because the events are
already arriving on the child's stdout.

A `hello` in `tui` mode is a session somebody started at a keyboard. It becomes
a `shared` session (A26): the branch it already holds is replayed as history,
and from then on it reads, writes, stops and re-models exactly like a Codex
thread under the shared daemon. When the process leaves, the session drops to
`control: "none"` and the next `session.send` resumes it with
`pi --mode rpc --session-id <id>` in the same working directory.
"""

from __future__ import annotations

import weakref
from typing import TYPE_CHECKING, Any

from ...logging_setup import logger
from ...models import Session, now_ms
from ...sessions import titles
from . import frames
from .adapter import PiRunner
from .catalog import DEFAULT_PERMISSION_MODE
from .history import replay
from .link import PiLink
from .terminal import PiTerminalSession

if TYPE_CHECKING:  # pragma: no cover - imported for types only
    from ...sessions.hub import SessionEntry, SessionHub

log = logger("rc_client.pi.service")


class PiExtensionService:
    """Maps pi extension connections onto hub sessions."""

    def __init__(self, hub: SessionHub) -> None:
        self.hub = hub
        # Runners the hub is starting, by the session id they told pi to use.
        # Weak, because a runner the hub has finished with is one whose child
        # has exited and whose extension will never dial again.
        self._expected: weakref.WeakValueDictionary[str, PiRunner] = weakref.WeakValueDictionary()
        self._links: dict[str, PiLink] = {}

    # ------------------------------------------------------- the RPC children

    def expect(self, runner: PiRunner) -> None:
        """A child is about to start; its extension will dial in a moment."""
        self._expected[runner.session_id] = runner

    # ------------------------------------------------------------ the socket

    async def link_registered(self, link: PiLink) -> bool:
        hello = link.hello
        if hello.mode == frames.RPC:
            runner = self._expected.get(hello.session_id)
            if runner is None:
                # A pi somebody started with `--mode rpc` of their own: there is
                # no session here to attach it to, and nothing to drive it with.
                return False
            runner.link = link
            await link.welcome(runner.permission_mode or "never", stream=False)
            return True
        return await self._register_terminal(link)

    async def link_frame(self, link: PiLink, frame: dict[str, Any]) -> None:
        kind = str(frame.get("type") or "")
        entry = self.hub.entries.get(link.session_id)
        runner = entry.runner if entry is not None else None
        if isinstance(runner, PiRunner):
            if kind == frames.ASK:
                await runner.approvals.ask(frame)
                await entry.channel.set_state("needs_approval")  # type: ignore[union-attr]
            elif kind == frames.ASK_CLOSED:
                await runner.approvals.closed(frame)
            return
        if isinstance(runner, PiTerminalSession) and runner.link is link:
            await runner.frame(frame)

    async def link_closed(self, link: PiLink) -> None:
        self._links.pop(link.session_id, None)
        entry = self.hub.entries.get(link.session_id)
        if entry is None:
            return
        runner = entry.runner
        if isinstance(runner, PiRunner):
            if runner.link is link:
                runner.link = None
                await runner.approvals.expire()
            return
        if not isinstance(runner, PiTerminalSession) or runner.link is not link:
            return
        # Amendment A10: the CLI is gone, so nobody owns the session now. The
        # next send resumes it under this device.
        await runner.gone()
        entry.runner = None
        entry.session.control = "none"
        await entry.channel.emit("meta", control="none")
        await entry.channel.set_state("idle")
        await entry.channel.publish_summary()

    # ------------------------------------------------------ terminal sessions

    async def _register_terminal(self, link: PiLink) -> bool:
        hello = link.hello
        previous = self._links.get(hello.session_id)
        if previous is not None and previous is not link:
            previous.detach()
        self._links[hello.session_id] = link
        entry, fresh = self._entry(hello.session_id, link)
        mode = entry.session.permission_mode or DEFAULT_PERMISSION_MODE
        runner = PiTerminalSession(
            entry.channel,
            link,
            permission_mode=mode,
            on_turn_end=lambda: self.hub.drain_queue(entry),
        )
        entry.runner = runner
        await entry.channel.revive()
        if fresh:
            await self._replay(entry, link)
        await self._publish_meta(entry, link)
        await link.welcome(mode, stream=True)
        return True

    def _entry(self, session_id: str, link: PiLink) -> tuple[SessionEntry, bool]:
        """The session this pi process is in, created the first time it is seen."""
        entry = self.hub.entries.get(session_id)
        if entry is not None:
            return entry, not self.hub.registry.has_events(session_id)
        hello = link.hello
        created = now_ms()
        entry = self.hub.register_mirrored(
            Session(
                session_id=session_id,
                device_id=self.hub.device_id,
                agent="pi",
                cwd=hello.cwd,
                title="",
                state="idle",
                origin="terminal",
                control="shared",
                model=hello.model,
                permission_mode=DEFAULT_PERMISSION_MODE,
                effort=hello.thinking,
                created_at=created,
                updated_at=created,
            )
        )
        entry.channel.start()
        return entry, True

    async def _replay(self, entry: SessionEntry, link: PiLink) -> None:
        """Publish what the session already held, once, in its own timestamps."""
        for emit in replay(link.hello.entries):
            await entry.channel.emit(emit.kind, **emit.fields)

    async def _publish_meta(self, entry: SessionEntry, link: PiLink) -> None:
        hello = link.hello
        entry.holder_pid = hello.pid or None
        entry.session.control = "shared"
        await entry.channel.emit("meta", control="shared")
        fields: dict[str, object] = {"cwd": hello.cwd} if hello.cwd else {}
        if hello.model:
            fields["model"] = hello.model
        if hello.thinking:
            fields["effort"] = hello.thinking
        if fields:
            await entry.channel.set_meta(**fields)
        if hello.name:
            await titles.from_agent(entry.channel, hello.name)
        else:
            await titles.from_prompt(entry.channel, _first_prompt(link))
        await entry.channel.publish_summary()


def _first_prompt(link: PiLink) -> str:
    """The first thing the person said, which is what pi titles a session by."""
    for entry in link.hello.entries:
        if not isinstance(entry, dict) or entry.get("type") != "message":
            continue
        message = entry.get("message")
        if isinstance(message, dict) and message.get("role") == "user":
            content = message.get("content")
            if isinstance(content, list):
                for item in content:
                    if isinstance(item, dict) and item.get("type") == "text":
                        return str(item.get("text") or "")
    return ""
