"""The daemon end of `pi-extension.sock`: one connection per pi process.

Anything that can write to this socket can inject prompts into a live agent and
approve its tool calls, so it lives inside the device home with owner-only
permissions, exactly as the Claude channel socket does.

A connection opens with one `hello` naming the pi session, its working
directory and the branch it already holds. What happens next depends on what
kind of pi is on the other end: an RPC child the device started is already
being driven over its own stdin, so its link carries nothing but approvals,
while a pi somebody started in a terminal is driven entirely from here.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from ...errors import RcError
from ...logging_setup import logger
from . import frames

log = logger("rc_client.pi.link")

HELLO_TIMEOUT = 10.0
COMMAND_TIMEOUT = 30.0
# How long a departing pi's last frames may take to finish being published.
HANDOVER_TIMEOUT = 10.0


@dataclass(slots=True)
class Hello:
    """What a pi process says about itself when it connects."""

    session_id: str
    cwd: str
    pid: int
    mode: str
    model: str | None
    thinking: str | None
    name: str | None
    session_file: str | None
    entries: list[Any] = field(default_factory=list)

    @classmethod
    def parse(cls, frame: dict[str, Any]) -> Hello | None:
        session_id = str(frame.get("session_id") or "")
        if not session_id:
            return None
        mode = str(frame.get("mode") or "")
        entries = frame.get("entries")
        return cls(
            session_id=session_id,
            cwd=str(frame.get("cwd") or ""),
            pid=int(frame.get("pid") or 0),
            mode=mode if mode in (frames.TUI, frames.RPC) else frames.RPC,
            model=_text(frame.get("model")),
            thinking=_text(frame.get("thinking")),
            name=_text(frame.get("name")),
            session_file=_text(frame.get("session_file")),
            entries=list(entries) if isinstance(entries, list) else [],
        )


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


class PiLink:
    """One live extension connection, and the commands that travel on it."""

    def __init__(self, hello: Hello, writer: asyncio.StreamWriter) -> None:
        self.hello = hello
        self._writer: asyncio.StreamWriter | None = writer
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._next = 1

    @property
    def alive(self) -> bool:
        return self._writer is not None

    @property
    def session_id(self) -> str:
        return self.hello.session_id

    async def welcome(self, permission_mode: str, stream: bool) -> None:
        await self._write(
            {
                "type": frames.WELCOME,
                "permission_mode": permission_mode,
                "stream": stream,
            }
        )

    async def command(
        self, command: str, timeout: float = COMMAND_TIMEOUT, **fields: Any
    ) -> dict[str, Any]:
        """Send one command and answer with its data, or raise what it refused.

        `timeout` is a keyword for the one command that waits on a model call
        of its own: compaction summarises the whole session before it answers.
        """
        request_id = f"c{self._next}"
        self._next += 1
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        payload = {"type": frames.COMMAND, "id": request_id, "command": command, **fields}
        if not await self._write(payload):
            self._pending.pop(request_id, None)
            raise RcError("agent_unavailable", "the pi session is no longer attached")
        try:
            reply = await asyncio.wait_for(future, timeout=timeout)
        except TimeoutError as exc:
            raise RcError("timeout", f"pi did not answer {command}") from exc
        finally:
            self._pending.pop(request_id, None)
        if not reply.get("ok"):
            raise RcError("bad_request", str(reply.get("error") or f"pi refused {command}"))
        data = reply.get("data")
        return data if isinstance(data, dict) else {}

    async def answer(self, ask_id: str, option_id: str) -> None:
        await self._write({"type": frames.ANSWER, "id": ask_id, "decision": option_id})

    def reply(self, frame: dict[str, Any]) -> None:
        future = self._pending.pop(str(frame.get("id") or ""), None)
        if future is not None and not future.done():
            future.set_result(frame)

    def detach(self) -> None:
        writer, self._writer = self._writer, None
        for future in self._pending.values():
            if not future.done():
                future.set_exception(RcError("agent_unavailable", "the pi session went away"))
        self._pending.clear()
        if writer is not None:
            writer.close()

    async def _write(self, frame: dict[str, Any]) -> bool:
        writer = self._writer
        if writer is None:
            return False
        try:
            writer.write(frames.encode(frame))
            await writer.drain()
        except (OSError, RuntimeError):
            self.detach()
            return False
        return True


class LinkSink(Protocol):
    """What the service has to provide for a connection to be useful."""

    async def link_registered(self, link: PiLink) -> bool: ...

    async def link_frame(self, link: PiLink, frame: dict[str, Any]) -> None: ...

    async def link_closed(self, link: PiLink) -> None: ...


class PiExtensionServer:
    """Accepts pi extensions and hands each registered one to the sink."""

    def __init__(self, path: Path, sink: LinkSink) -> None:
        self.path = path
        self._sink = sink
        self._server: asyncio.AbstractServer | None = None
        self._connections: set[asyncio.Task[None]] = set()

    async def start(self) -> None:
        self._prepare_directory()
        self._unlink_stale()
        self._server = await asyncio.start_unix_server(self._serve, path=str(self.path))
        os.chmod(self.path, 0o600)
        log.info("pi extension socket listening", path=str(self.path))

    def _prepare_directory(self) -> None:
        """Own the directory outright: whoever can write here can drive an agent."""
        directory = self.path.parent
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        info = directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            raise OSError(f"{directory} is not a directory this user owns")
        if stat.S_IMODE(info.st_mode) & 0o077:
            os.chmod(directory, 0o700)

    def _unlink_stale(self) -> None:
        with contextlib.suppress(FileNotFoundError, OSError):
            if self.path.is_socket():
                self.path.unlink()

    async def stop(self) -> None:
        server, self._server = self._server, None
        if server is not None:
            server.close()
            with contextlib.suppress(Exception):
                await server.wait_closed()
        for task in list(self._connections):
            task.cancel()
        for task in list(self._connections):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._connections.clear()
        with contextlib.suppress(FileNotFoundError, OSError):
            self.path.unlink()

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._connections.add(task)
        try:
            await self._session(reader, writer)
        except (asyncio.IncompleteReadError, ConnectionError, OSError):
            pass
        finally:
            if task is not None:
                self._connections.discard(task)
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def _session(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """Read frames here, handle them there.

        A handler may send a command of its own — the totals a turn ends with,
        for one — and only the reader can deliver the reply to it, so handling
        a frame on the reading task would wait for itself for ever.
        """
        link = await self._register(reader, writer)
        if link is None:
            return
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        worker = asyncio.create_task(self._handle(link, queue))
        try:
            async for line in reader:
                frame = frames.decode(line)
                if frame is None:
                    continue
                if frame.get("type") == frames.REPLY:
                    link.reply(frame)
                    continue
                await queue.put(frame)
                if frame.get("type") == frames.BYE:
                    break
        finally:
            link.detach()
            # Everything pi already said is handled before it is declared gone.
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await asyncio.wait_for(queue.join(), timeout=HANDOVER_TIMEOUT)
            worker.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await worker
            await self._sink.link_closed(link)

    async def _handle(self, link: PiLink, queue: asyncio.Queue[dict[str, Any]]) -> None:
        """One frame at a time, in the order pi sent them."""
        while True:
            frame = await queue.get()
            try:
                await self._sink.link_frame(link, frame)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("a pi extension frame could not be handled")
            finally:
                queue.task_done()

    async def _register(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> PiLink | None:
        """The first frame must be a `hello` naming the session, or we hang up."""
        try:
            line = await asyncio.wait_for(reader.readline(), timeout=HELLO_TIMEOUT)
        except (TimeoutError, OSError):
            return None
        frame = frames.decode(line)
        hello = Hello.parse(frame) if frame and frame.get("type") == frames.HELLO else None
        if hello is None:
            return None
        link = PiLink(hello, writer)
        if not await self._sink.link_registered(link):
            link.detach()
            return None
        return link
