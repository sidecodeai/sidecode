"""Materialise `session.send` attachments as files the agent can read.

Every agent reads files from disk far more reliably than it accepts inline
binary payloads, so an attachment becomes a file under the client state
directory and the prompt gains a short list of the resulting paths. Claude
Code and Grok Build get nothing else: they open the files with their own file
tools (A49).

Each message has a folder of its own, `<session>/<message>/<name>`, so two
files with one name never overwrite each other, and nothing stays for long. A
message that will never reach its agent takes its folder with it, a deleted
session takes all of its folders, and `sweep` deletes every file older than a
week and then, oldest first, whatever keeps the root over its size cap. None of
it follows a link or leaves the root, and none of it logs more than counts and
bytes: the names and the paths are what the person sent.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import mimetypes
import os
import re
import stat
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import state_dir
from .errors import RcError
from .logging_setup import logger

log = logger("rc_client.attachments")

MAX_ATTACHMENTS = 8
MAX_ATTACHMENT_BYTES = 6 * 1024 * 1024
# How long a file is kept, and how much the root may hold before the oldest go.
MAX_AGE = 7 * 24 * 3600.0
MAX_TOTAL_BYTES = 512 * 1024 * 1024
# The daemon sweeps once when it starts and then this often.
SWEEP_INTERVAL = 3600.0
# A folder changed this recently may be one a send is filling right now.
FRESH = 60.0
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")
_EXTENSION = re.compile(r"[A-Za-z0-9]{1,15}")
_HEADING = "Attached files:\n"
_LISTED = re.compile(r"- ([A-Za-z0-9._-]+): (.+)")
LINKED_ROOT = "attachments are refused: the attachments folder on this device is a link"


@dataclass(frozen=True, slots=True)
class Attachment:
    name: str
    mime: str
    size: int
    path: str


@dataclass(frozen=True, slots=True)
class Swept:
    """What one sweep deleted, as counts and bytes only."""

    files: int = 0
    bytes: int = 0


@dataclass(frozen=True, slots=True)
class _Found:
    path: str
    mtime: float
    size: int = 0


@dataclass(slots=True)
class _Tree:
    """Everything under one folder; each folder is listed before what it holds."""

    files: list[_Found] = field(default_factory=list)
    folders: list[_Found] = field(default_factory=list)


def state_root() -> Path:
    """The folder every attachment lives under, in a state directory resolved once.

    The state directory may be reached through links — a home on another
    volume — so every path written, swept, cleaned up or recognised starts from
    one absolute place. The root itself has to be a real folder: if it is a
    link, attachments are refused and nothing goes through it, and below it
    nothing is ever followed.
    """
    return Path(os.path.realpath(state_dir())) / "attachments"


def _safe_name(name: str, index: int) -> str:
    """A name of safe characters that keeps the extension an agent tells images by."""
    stem, _, extension = name.strip().rpartition(".")
    if not _EXTENSION.fullmatch(extension):
        stem, extension = name.strip(), ""
    cleaned = _SAFE_NAME.sub("_", stem)[:64].strip("._-") or f"attachment-{index}"
    return f"{cleaned}.{extension}" if extension else cleaned


def _unique(name: str, taken: set[str]) -> str:
    """`name`, numbered when another file of the same message already has it."""
    if name not in taken:
        return name
    path = Path(name)
    number = 2
    while f"{path.stem}-{number}{path.suffix}" in taken:
        number += 1
    return f"{path.stem}-{number}{path.suffix}"


def _decoded(item: dict[str, Any]) -> bytes:
    raw = item.get("data_base64")
    if not isinstance(raw, str):
        raise RcError("bad_request", "attachment is missing data_base64")
    try:
        payload = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise RcError("bad_request", "attachment is not valid base64") from exc
    if len(payload) > MAX_ATTACHMENT_BYTES:
        raise RcError("too_large", "attachment exceeds 6 MiB")
    return payload


def _checked(attachments: list[dict[str, Any]]) -> list[tuple[str, str, bytes]]:
    """Every attachment's file name, type and bytes, before anything is written."""
    if len(attachments) > MAX_ATTACHMENTS:
        raise RcError("too_large", f"at most {MAX_ATTACHMENTS} attachments per message")
    checked: list[tuple[str, str, bytes]] = []
    taken: set[str] = set()
    for index, item in enumerate(attachments):
        payload = _decoded(item)
        name = _unique(_safe_name(str(item.get("name") or ""), index), taken)
        taken.add(name)
        checked.append((name, str(item.get("mime") or "application/octet-stream"), payload))
    return checked


def materialise(session_id: str, attachments: list[dict[str, Any]]) -> list[Attachment]:
    """Write one message's attachments into a new folder, computing each decoded size.

    A message refused for one bad file leaves nothing behind: every file is
    decoded and checked before the folder is made.
    """
    checked = _checked(attachments)
    if not checked:
        return []
    if state_root().is_symlink():
        raise RcError("internal", LINKED_ROOT)
    target: Path | None = None
    try:
        target = _message_dir(session_id)
        return [_write(target / name, mime, payload) for name, mime, payload in checked]
    except OSError as exc:
        if target is not None:
            _remove_tree(target)
        raise RcError("internal", "could not store the attachments") from exc


async def store(session_id: str, uploads: list[dict[str, Any]] | None) -> list[Attachment]:
    """`materialise`, off the event loop: decoding and writing take their time.

    A send cancelled meanwhile, as a close stops a drain, leaves no files: the
    thread cannot be stopped, so its write is let finish and taken back.
    """
    if not uploads:
        return []
    writing = asyncio.ensure_future(asyncio.to_thread(materialise, session_id, uploads))
    try:
        return await asyncio.shield(writing)
    except asyncio.CancelledError:
        with contextlib.suppress(Exception):
            await drop(await writing)
        raise


async def drop(written: list[Attachment]) -> None:
    """`discard`, off the event loop."""
    if written:
        await asyncio.to_thread(discard, written)


def present(written: list[Attachment]) -> bool:
    """Whether every file of a message is still there, as the file it was written as."""
    for item in written:
        try:
            if not stat.S_ISREG(os.lstat(item.path).st_mode):
                return False
        except (OSError, ValueError):
            return False
    return True


def _write(path: Path, mime: str, payload: bytes) -> Attachment:
    """A new owner-only file, never written through a link or over another file."""
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(payload)
    return Attachment(name=path.name, mime=mime, size=len(payload), path=str(path))


def wire_attachments(written: list[Attachment]) -> list[dict[str, Any]]:
    """The `{name, mime, size}` form carried by a `user_message` event."""
    return [{"name": item.name, "mime": item.mime, "size": item.size} for item in written]


def describe(prompt: str, written: list[Attachment]) -> str:
    """Append the attachment paths to a prompt so the agent can open them."""
    if not written:
        return prompt
    lines = "\n".join(f"- {item.name}: {item.path}" for item in written)
    suffix = f"\n\n{_HEADING}{lines}"
    return (prompt + suffix) if prompt else suffix.lstrip()


def split_prompt(text: str) -> tuple[str, list[dict[str, Any]]]:
    """Undo `describe` for a prompt read back from an agent's own records.

    A transcript, an update log and the leader's echo hold the prompt the agent
    was given, paths and all; the apps get the words that were typed and the
    files in the `{name, mime, size}` form, and never a path. Text whose last
    lines do not name files under the attachments root is somebody's own words
    and comes back whole.
    """
    head, found, listing = text.rpartition("\n\n" + _HEADING)
    if not found:
        if not text.startswith(_HEADING):
            return text, []
        head, listing = "", text[len(_HEADING) :]
    if state_root().is_symlink():
        return text, []
    root = str(state_root()) + os.sep
    files: list[dict[str, Any]] = []
    for line in listing.split("\n"):
        item = _listed(line, root)
        if item is None:
            return text, []
        files.append(item)
    return head, files


def without_paths(text: str) -> str:
    """`text` with every path under the attachments root cut down to its file name.

    An agent's own words about a session — the title it gives it, the error it
    reports — may quote the prompt it was given; the apps get a file's name and
    never where it lives.
    """
    root = str(state_root()) + os.sep
    if root not in text:
        return text
    return re.sub(
        re.escape(root) + r"[A-Za-z0-9._/-]*",
        lambda found: os.path.basename(found.group(0).rstrip("/")),
        text,
    )


def _listed(line: str, root: str) -> dict[str, Any] | None:
    """One `- name: path` line of a `describe` suffix, when the path is ours."""
    match = _LISTED.fullmatch(line)
    if match is None:
        return None
    name, path = match.groups()
    if not os.path.isabs(path) or not os.path.normpath(path).startswith(root):
        return None
    mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
    return {"name": name, "mime": mime, "size": _size(path)}


def _size(path: str) -> int:
    """The size of a file that is still there; one a sweep took reads as 0."""
    try:
        info = os.stat(path, follow_symlinks=False)
    except (OSError, ValueError):
        return 0
    return info.st_size if stat.S_ISREG(info.st_mode) else 0


def _session_dir(session_id: str) -> Path:
    return state_root() / (_SAFE_NAME.sub("_", session_id)[:64].strip(".") or "_")


def is_session_file(session_id: str, path: Any) -> bool:
    """Whether `path` names a file the device wrote for this session's messages.

    An agent opening one is reading what its prompt named, so it asks nobody
    (A49). The path must be the one the device wrote — absolute, already
    normal — in the session's own folder; nothing from the root down to the
    file may be a link, and the file may have no other name, so neither a link
    nor a hard link planted there ever stands in for a file somewhere else.
    """
    if not isinstance(path, str) or "\x00" in path:
        return False
    if not os.path.isabs(path) or os.path.normpath(path) != path:
        return False
    folder = _session_dir(session_id)
    try:
        relative = Path(path).relative_to(folder)
    except ValueError:
        return False
    if folder.parent.is_symlink():
        return False
    current, info = folder.parent, None
    for part in (folder.name, *relative.parts):
        current = current / part
        try:
            info = current.lstat()
        except OSError:
            return False
        if stat.S_ISLNK(info.st_mode):
            return False
    return info is not None and stat.S_ISREG(info.st_mode) and info.st_nlink == 1


def _private_dir(path: Path) -> None:
    """Make `path` an owner-only folder, refusing a link where one belongs."""
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink():
        raise NotADirectoryError(f"{path.name} is a link, not a folder")
    path.chmod(0o700)


def _new_dir(session: Path) -> Path:
    _private_dir(session.parent)
    _private_dir(session)
    target = session / uuid.uuid4().hex[:12]
    target.mkdir(mode=0o700)
    return target


def _message_dir(session_id: str) -> Path:
    """A new owner-only folder for one message, inside its session's folder."""
    session = _session_dir(session_id)
    try:
        return _new_dir(session)
    except (FileExistsError, FileNotFoundError):
        # Another message has that name, or a sweep took the session's empty
        # folder a moment ago: either way a second try settles it.
        return _new_dir(session)


def rekey(old_id: str, new_id: str) -> None:
    """Follow a session whose provisional id was replaced by the agent's own."""
    source = _session_dir(old_id)
    if old_id == new_id or source.parent.is_symlink() or not source.is_dir():
        return
    target = _session_dir(new_id)
    if target.exists():
        return
    source.rename(target)


def cleanup(session_id: str) -> None:
    """Delete every file a session's messages left, once the session is deleted."""
    if not state_root().is_symlink():
        _remove_tree(_session_dir(session_id))


def discard(written: list[Attachment]) -> None:
    """Delete the folder of a message that will not reach its agent now."""
    root = state_root()
    if root.is_symlink():
        return
    for folder in {Path(item.path).parent for item in written}:
        if folder.parent.parent == root:
            _remove_tree(folder)


def sweep(now: float | None = None, cap: int = MAX_TOTAL_BYTES) -> Swept:
    """Delete files older than `MAX_AGE`, then the oldest until the root holds `cap`.

    Files written by earlier versions straight into a session's folder are
    swept like any other. A file that vanishes meanwhile is simply not counted.
    """
    root = state_root()
    if root.is_symlink() or not root.is_dir():
        return Swept()
    moment = time.time() if now is None else now
    tree = _scan(root)
    cutoff = moment - MAX_AGE
    held = sum(found.size for found in tree.files)
    files = removed = 0
    for found in sorted(tree.files, key=lambda item: item.mtime):
        if found.mtime >= cutoff and held <= cap:
            break
        held -= found.size
        if _unlink(found.path):
            files += 1
            removed += found.size
    _remove_empty(tree.folders, moment)
    if files:
        log.info("attachments swept", files=files, bytes=removed)
    return Swept(files=files, bytes=removed)


def _scan(top: Path) -> _Tree:
    """Everything under `top`, read without following a single link."""
    tree = _Tree()
    pending = [str(top)]
    while pending:
        try:
            with os.scandir(pending.pop()) as entries:
                for entry in entries:
                    _note(entry, tree, pending)
        except OSError:
            # Gone already, or not ours to read: there is nothing to delete.
            continue
    return tree


def _note(entry: os.DirEntry[str], tree: _Tree, pending: list[str]) -> None:
    try:
        info = entry.stat(follow_symlinks=False)
    except OSError:
        return
    if stat.S_ISDIR(info.st_mode):
        tree.folders.append(_Found(entry.path, info.st_mtime))
        pending.append(entry.path)
    else:
        tree.files.append(_Found(entry.path, info.st_mtime, info.st_size))


def _remove_empty(folders: list[_Found], moment: float) -> None:
    """Remove the folders left empty, deepest first, except ones a send may be filling."""
    for folder in reversed(folders):
        if folder.mtime < moment - FRESH:
            _rmdir(folder.path)


def _remove_tree(top: Path) -> None:
    """Delete a folder and everything in it; a link is removed, never followed."""
    try:
        info = top.lstat()
    except OSError:
        return
    if not stat.S_ISDIR(info.st_mode):
        _unlink(str(top))
        return
    tree = _scan(top)
    for found in tree.files:
        _unlink(found.path)
    for folder in reversed(tree.folders):
        _rmdir(folder.path)
    _rmdir(str(top))


def _unlink(path: str) -> bool:
    try:
        os.unlink(path)
    except OSError:
        return False
    return True


def _rmdir(path: str) -> None:
    try:
        os.rmdir(path)
    except OSError:
        # Not empty, or gone already: either way it is not ours to remove.
        return
