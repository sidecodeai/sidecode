"""Stable UUID v4 shaped block ids.

Agents name their blocks in their own way (`msg_01…:0`, `toolu_01…`,
`exec-…`), but the protocol schema requires every `block_id` to look like a
UUID v4. Derive one deterministically so a later event about the same agent
block always carries the same id, on this run and after a restart.
"""

from __future__ import annotations

import uuid

BLOCK_NAMESPACE = uuid.UUID("6f2b9f4c-2a7e-4d2f-9b6a-1f3f5a0c7d11")
_UUID_V4_LENGTH = 36


def is_uuid_v4(value: str) -> bool:
    if len(value) != _UUID_V4_LENGTH:
        return False
    try:
        parsed = uuid.UUID(value)
    except ValueError:
        return False
    return parsed.version == 4 and str(parsed) == value.lower()


def block_uuid(natural_id: str) -> str:
    """Map any agent-native block identifier onto one stable UUID v4."""
    if is_uuid_v4(natural_id):
        return natural_id
    digest = uuid.uuid5(BLOCK_NAMESPACE, natural_id)
    raw = bytearray(digest.bytes)
    raw[6] = (raw[6] & 0x0F) | 0x40
    raw[8] = (raw[8] & 0x3F) | 0x80
    return str(uuid.UUID(bytes=bytes(raw)))
