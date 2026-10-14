"""Session event construction and the size bounds from PROTOCOL §4.

Every event leaves the device in two forms: the bounded *wire* copy that goes
into `session.event` frames and `session.history`, and the full copy kept in the
registry so `session.block` can return it untruncated (up to 1 MiB).
"""

from __future__ import annotations

import copy
import json
from typing import Any

MAX_INPUT_BYTES = 8 * 1024
MAX_OUTPUT_BYTES = 16 * 1024
MAX_PATCH_BYTES = 32 * 1024
MAX_EVENT_BYTES = 64 * 1024
MAX_BLOCK_BYTES = 1024 * 1024
DELTA_FLUSH_MS = 80

TRUNCATION_MARKER = "\n…[truncated]"

# Event kinds that `session.history` replays (PROTOCOL §8). Streaming deltas and
# the state-carrying kinds (`status`, `meta`, `queue`) are never stored.
HISTORY_KINDS = frozenset(
    {
        "user_message",
        "assistant_text",
        "thinking",
        "tool_call",
        "approval",
        "question",
        "todos",
        "turn_started",
        "turn_completed",
        "notice",
        "error",
        # Amendment A35: like `notice`, a `resume` changes no state and reads as
        # a row of the timeline, so a reload and the gateway's backfill must
        # both be able to replay it.
        "resume",
    }
)
# Kinds whose latest event replaces the previous one under the same key.
BLOCK_KINDS = frozenset(
    {"user_message", "assistant_text", "thinking", "tool_call", "approval", "question"}
)


def truncate_text(value: str, limit: int) -> tuple[str, bool]:
    """Cut `value` to `limit` UTF-8 bytes on a character boundary."""
    encoded = value.encode("utf-8")
    if len(encoded) <= limit:
        return value, False
    room = max(0, limit - len(TRUNCATION_MARKER.encode("utf-8")))
    return encoded[:room].decode("utf-8", "ignore") + TRUNCATION_MARKER, True


def _json_size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, default=str).encode("utf-8"))


def bound_object(value: dict[str, Any], limit: int) -> tuple[dict[str, Any], bool]:
    """Shrink an object until its JSON encoding fits `limit` bytes.

    Long string values are truncated first (longest first) because they carry
    most of the size; if that is not enough, trailing keys are dropped.
    """
    if _json_size(value) <= limit:
        return value, False
    result = copy.deepcopy(value)
    truncated = False
    for _ in range(64):
        if _json_size(result) <= limit:
            break
        longest_key: str | None = None
        longest_len = 0
        for key, item in result.items():
            length = len(item) if isinstance(item, str) else 0
            if length > longest_len:
                longest_key, longest_len = key, length
        if longest_key is None or longest_len < 64:
            break
        current = result[longest_key]
        assert isinstance(current, str)
        shrunk, _ = truncate_text(current, max(64, len(current.encode("utf-8")) // 2))
        result[longest_key] = shrunk
        truncated = True
    while _json_size(result) > limit and result:
        result.pop(next(reversed(result)))
        truncated = True
    return result, truncated


def bound_event(event: dict[str, Any]) -> dict[str, Any]:
    """Return the wire copy of `event` with §4 bounds applied."""
    bounded = dict(event)
    raw_input = bounded.get("input")
    if isinstance(raw_input, dict):
        value, truncated = bound_object(raw_input, MAX_INPUT_BYTES)
        bounded["input"] = value
        if truncated:
            bounded["input_truncated"] = True
    output = bounded.get("output")
    if isinstance(output, str):
        value_text, truncated = truncate_text(output, MAX_OUTPUT_BYTES)
        bounded["output"] = value_text
        if truncated:
            bounded["output_truncated"] = True
    diff = bounded.get("diff")
    if isinstance(diff, dict) and isinstance(diff.get("patch"), str):
        patch, truncated = truncate_text(diff["patch"], MAX_PATCH_BYTES)
        bounded["diff"] = dict(diff, patch=patch)
        if truncated:
            bounded["diff"]["patch_truncated"] = True
    text = bounded.get("text")
    if isinstance(text, str):
        value_text, truncated = truncate_text(text, MAX_EVENT_BYTES // 2)
        if truncated:
            bounded["text"] = value_text
    return _fit_frame(bounded)


def _fit_frame(bounded: dict[str, Any]) -> dict[str, Any]:
    """Shrink the whole event until it fits the 64 KiB frame the gateway accepts.

    The per-field caps add up to more than one frame once JSON escaping is
    counted, and the gateway drops anything larger, so squeeze the big string
    fields again from the largest down.
    """
    for _ in range(8):
        excess = _json_size(bounded) - MAX_EVENT_BYTES
        if excess <= 0:
            return bounded
        for key in ("output", "text"):
            value = bounded.get(key)
            if isinstance(value, str) and len(value.encode("utf-8")) > 512:
                limit = max(512, len(value.encode("utf-8")) - excess - 256)
                bounded[key], truncated = truncate_text(value, limit)
                if truncated and key == "output":
                    bounded["output_truncated"] = True
                break
        else:
            diff = bounded.get("diff")
            if isinstance(diff, dict) and isinstance(diff.get("patch"), str):
                patch = diff["patch"]
                limit = max(512, len(patch.encode("utf-8")) - excess - 256)
                trimmed, truncated = truncate_text(patch, limit)
                bounded["diff"] = dict(diff, patch=trimmed)
                if truncated:
                    bounded["diff"]["patch_truncated"] = True
                continue
            raw_input = bounded.get("input")
            if isinstance(raw_input, dict) and raw_input:
                shrunk, truncated = bound_object(raw_input, max(512, MAX_INPUT_BYTES - excess))
                bounded["input"] = shrunk
                if truncated:
                    bounded["input_truncated"] = True
                continue
            break
    return bounded


def bound_block(event: dict[str, Any]) -> dict[str, Any]:
    """Clamp a stored block to the 1 MiB `session.block` ceiling."""
    if _json_size(event) <= MAX_BLOCK_BYTES:
        return event
    bounded = dict(event)
    for key in ("output", "text"):
        value = bounded.get(key)
        if isinstance(value, str):
            bounded[key], truncated = truncate_text(value, MAX_BLOCK_BYTES // 2)
            if truncated and key == "output":
                bounded["output_truncated"] = True
    diff = bounded.get("diff")
    if isinstance(diff, dict) and isinstance(diff.get("patch"), str):
        patch, truncated = truncate_text(diff["patch"], MAX_BLOCK_BYTES // 4)
        bounded["diff"] = dict(diff, patch=patch)
        if truncated:
            bounded["diff"]["patch_truncated"] = True
    if _json_size(bounded) > MAX_BLOCK_BYTES and isinstance(bounded.get("input"), dict):
        bounded["input"], truncated = bound_object(bounded["input"], MAX_BLOCK_BYTES // 4)
        if truncated:
            bounded["input_truncated"] = True
    return bounded


def dedup_key(event: dict[str, Any]) -> str | None:
    """The registry key under which a later event replaces an earlier one."""
    kind = event.get("kind")
    if kind in BLOCK_KINDS:
        block_id = event.get("block_id")
        return f"block:{block_id}" if block_id else None
    if kind == "todos":
        return "todos"
    if kind in HISTORY_KINDS:
        return f"seq:{event['seq']}"
    return None


def should_store(event: dict[str, Any]) -> bool:
    """True when `session.history` must be able to replay this event later."""
    kind = event.get("kind")
    if kind not in HISTORY_KINDS:
        return False
    if kind in {"assistant_text", "thinking"}:
        return bool(event.get("done"))
    return True
