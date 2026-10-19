"""Attachments as the apps upload them, built from the protocol's own fixture.

Every upload a test sends starts from `fixtures/app/session.send.json` and is
checked against the `AttachmentUpload` schema, so a test can never feed an
adapter a field the wire does not carry: pi once read `data` where every app
sends `data_base64`, and its tests, which sent `data`, stayed green.
"""

from __future__ import annotations

import base64
from typing import Any

from tests.helpers import load_fixture, object_validator


def upload(name: str | None = None, mime: str | None = None) -> dict[str, Any]:
    """The fixture's image, or the same bytes under another name and type."""
    item = dict(load_fixture("app/session.send.json")["attachments"][0])
    if name is not None:
        item["name"] = name
    if mime is not None:
        item["mime"] = mime
    validator = object_validator("AttachmentUpload")
    assert validator is not None, "protocol/schema/objects.json is missing"
    validator.validate(item)
    return item


def shown(item: dict[str, Any]) -> dict[str, Any]:
    """What a `user_message` says of an upload: its name, type and decoded size."""
    size = len(base64.b64decode(item["data_base64"]))
    return {"name": item["name"], "mime": item["mime"], "size": size}
