"""Materialise `session.send` attachments as files the agent can read.

Both agents read files from disk far more reliably than they accept inline
binary payloads, so an attachment becomes a file under the client state
directory and the prompt gains a short list of the resulting paths.
"""

from __future__ import annotations

import base64
import binascii
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import state_dir
from .errors import RcError

MAX_ATTACHMENTS = 8
MAX_ATTACHMENT_BYTES = 6 * 1024 * 1024
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass(frozen=True, slots=True)
class Attachment:
    name: str
    mime: str
    size: int
    path: str


def _safe_name(name: str, index: int) -> str:
    cleaned = _SAFE_NAME.sub("_", name.strip())[:80].strip("._-")
    return cleaned or f"attachment-{index}"


def materialise(session_id: str, attachments: list[dict[str, Any]]) -> list[Attachment]:
    """Write attachments to disk, computing the decoded size of each."""
    if len(attachments) > MAX_ATTACHMENTS:
        raise RcError("too_large", f"at most {MAX_ATTACHMENTS} attachments per message")
    target = _session_dir(session_id)
    target.mkdir(parents=True, exist_ok=True, mode=0o700)
    written: list[Attachment] = []
    for index, item in enumerate(attachments):
        raw = item.get("data_base64")
        if not isinstance(raw, str):
            raise RcError("bad_request", "attachment is missing data_base64")
        try:
            payload = base64.b64decode(raw, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise RcError("bad_request", "attachment is not valid base64") from exc
        if len(payload) > MAX_ATTACHMENT_BYTES:
            raise RcError("too_large", "attachment exceeds 6 MiB")
        name = _safe_name(str(item.get("name") or ""), index)
        path = target / name
        path.write_bytes(payload)
        path.chmod(0o600)
        written.append(
            Attachment(
                name=name,
                mime=str(item.get("mime") or "application/octet-stream"),
                size=len(payload),
                path=str(path),
            )
        )
    return written


def wire_attachments(written: list[Attachment]) -> list[dict[str, Any]]:
    """The `{name, mime, size}` form carried by a `user_message` event."""
    return [{"name": item.name, "mime": item.mime, "size": item.size} for item in written]


def describe(prompt: str, written: list[Attachment]) -> str:
    """Append the attachment paths to a prompt so the agent can open them."""
    if not written:
        return prompt
    lines = "\n".join(f"- {item.name}: {item.path}" for item in written)
    suffix = f"\n\nAttached files:\n{lines}"
    return (prompt + suffix) if prompt else suffix.lstrip()


def _session_dir(session_id: str) -> Path:
    return state_dir() / "attachments" / _SAFE_NAME.sub("_", session_id)[:64]


def rekey(old_id: str, new_id: str) -> None:
    """Follow a session whose provisional id was replaced by the agent's own."""
    source = _session_dir(old_id)
    if old_id == new_id or not source.is_dir():
        return
    target = _session_dir(new_id)
    if target.exists():
        return
    source.rename(target)


def cleanup(session_id: str) -> None:
    target = _session_dir(session_id)
    if not target.is_dir():
        return
    for child in target.iterdir():
        child.unlink(missing_ok=True)
    target.rmdir()


def state_root() -> Path:
    return state_dir() / "attachments"
