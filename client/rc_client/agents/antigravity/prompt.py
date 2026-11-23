"""A message as the content of an ACP prompt: the person's words, then their files (4.8, A64).

`agy` takes an image as image content, so its bytes go as they were uploaded and nothing is
written. Every other file the device writes under its own state, as for every agent (A49): a text
file then goes with its text embedded in the prompt, so `agy` needs no tool to read it, and any
other file as a link to the file written (`resource_link`), whose read the device allows itself.
The words are the person's own, unchanged: nothing is added to them.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ...attachments import MAX_ATTACHMENT_BYTES, Attachment, store
from ...errors import RcError

# The images `agy`'s models read; any other picture goes as a file.
IMAGE_TYPES = frozenset(
    {"image/png", "image/jpeg", "image/webp", "image/gif", "image/heic", "image/heif"}
)
# Text files that are not `text/*`, read as text when they are UTF-8.
TEXT_TYPES = frozenset(
    {
        "application/json",
        "application/xml",
        "application/x-yaml",
        "application/yaml",
        "application/toml",
        "application/javascript",
        "application/x-sh",
        "application/sql",
    }
)


@dataclass(slots=True)
class Prompt:
    """One message, as `agy` is given it and as its bubble names it."""

    blocks: list[dict[str, Any]] = field(default_factory=list)
    # The `Attachment` metadata of the message's `user_message`, in the order uploaded.
    shown: list[dict[str, Any]] = field(default_factory=list)
    # The files the device wrote for it, which a message `agy` never took takes back.
    written: list[Attachment] = field(default_factory=list)


async def build(session_id: str, text: str, uploads: list[dict[str, Any]] | None) -> Prompt:
    """The prompt for `text` and `uploads`; an upload that is not valid refuses the message."""
    items = list(uploads or [])
    images = {index: _image(item) for index, item in enumerate(items) if _is_image(item)}
    files = [item for index, item in enumerate(items) if index not in images]
    written = await store(session_id, files)
    linked = iter(await asyncio.to_thread(lambda: [_file(file) for file in written]))
    stored = iter(written)
    prompt = Prompt(written=written)
    if text:
        prompt.blocks.append({"type": "text", "text": text})
    for index in range(len(items)):
        image = images.get(index)
        if image is not None:
            block, shown = image
        else:
            file = next(stored)
            block, shown = next(linked), {"name": file.name, "mime": file.mime, "size": file.size}
        prompt.blocks.append(block)
        prompt.shown.append(shown)
    return prompt


def _is_image(item: dict[str, Any]) -> bool:
    return str(item.get("mime") or "").lower() in IMAGE_TYPES


def _image(item: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    mime = str(item.get("mime") or "").lower()
    data = item.get("data_base64")
    if not isinstance(data, str) or not data:
        raise RcError("bad_request", "attachment is missing data_base64")
    try:
        size = len(base64.b64decode(data, validate=True))
    except (binascii.Error, ValueError) as exc:
        raise RcError("bad_request", "attachment is not valid base64") from exc
    if size > MAX_ATTACHMENT_BYTES:
        raise RcError("too_large", "attachment exceeds 6 MiB")
    name = str(item.get("name") or "image")
    return {"type": "image", "mimeType": mime, "data": data}, {
        "name": name,
        "mime": mime,
        "size": size,
    }


def _file(file: Attachment) -> dict[str, Any]:
    uri = Path(file.path).as_uri()
    text = _text(file)
    if text is not None:
        return {"type": "resource", "resource": {"uri": uri, "mimeType": file.mime, "text": text}}
    return {
        "type": "resource_link",
        "uri": uri,
        "name": file.name,
        "mimeType": file.mime,
        "size": file.size,
    }


def _text(file: Attachment) -> str | None:
    """The file's text when it is a text file in UTF-8; None for anything else."""
    mime = file.mime.lower().split(";", 1)[0].strip()
    if not mime.startswith("text/") and mime not in TEXT_TYPES:
        return None
    try:
        return Path(file.path).read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return None
