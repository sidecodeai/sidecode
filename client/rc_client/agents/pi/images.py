"""What pi takes of a message's attachments: images, and nothing else (A26).

pi's `prompt` and its extension both take an image as base64 content, so the
bytes an app uploaded (`AttachmentUpload`, 4.8) go to pi as they arrived and
nothing is written to disk. Any other file refuses the whole message with
`unsupported` before a word of it is sent: pi never gets the words without the
files they came with.
"""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass, field
from typing import Any

from ...attachments import MAX_ATTACHMENT_BYTES
from ...errors import RcError

IMAGE_PREFIX = "image/"
REFUSAL = "pi sessions take images and no other attachment"


@dataclass(frozen=True, slots=True)
class PiImages:
    """One message's images, as pi takes them and as its bubble names them."""

    # pi's own `ImageContent`: `{type, data, mimeType}`.
    content: list[dict[str, str]] = field(default_factory=list)
    # The `Attachment` metadata of the message's `user_message`.
    shown: list[dict[str, Any]] = field(default_factory=list)

    def prompt(self) -> dict[str, Any]:
        """The `images` field of an RPC `prompt`, when there is anything to send."""
        return {"images": self.content} if self.content else {}

    def bubble(self) -> dict[str, Any]:
        """The `attachments` field of the `user_message`, when there are any."""
        return {"attachments": self.shown} if self.shown else {}


def pi_images(attachments: list[dict[str, Any]]) -> PiImages:
    """A message's uploads as pi's `ImageContent`, or the refusal of the message.

    The shape is pi's own `{type, data, mimeType}`, verified against a live
    session rather than taken from the documentation, which also shows an
    older nested `source` form that pi no longer accepts.
    """
    content: list[dict[str, str]] = []
    shown: list[dict[str, Any]] = []
    for item in attachments:
        mime = str(item.get("mime") or "")
        if not mime.startswith(IMAGE_PREFIX):
            raise RcError("unsupported", REFUSAL)
        data = item.get("data_base64")
        size = _decoded_size(data)
        content.append({"type": "image", "data": str(data), "mimeType": mime})
        shown.append({"name": str(item.get("name") or "image"), "mime": mime, "size": size})
    return PiImages(content, shown)


def _decoded_size(data: Any) -> int:
    """How many bytes one upload holds, refusing one that is not base64 or too big."""
    if not isinstance(data, str) or not data:
        raise RcError("bad_request", "attachment is missing data_base64")
    try:
        size = len(base64.b64decode(data, validate=True))
    except (binascii.Error, ValueError) as exc:
        raise RcError("bad_request", "attachment is not valid base64") from exc
    if size > MAX_ATTACHMENT_BYTES:
        raise RcError("too_large", "attachment exceeds 6 MiB")
    return size
