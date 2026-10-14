"""Frame vocabulary the gateway understands.

The gateway parses only what it routes: the envelope (``type``, ``id``), the addressing fields
(``device_id``, ``session_id``), ``seq`` on events and the ``Session`` summaries it indexes.
Everything else is forwarded byte-for-byte as it arrived. Unknown fields are preserved, never
rejected (protocol preamble).
"""

from __future__ import annotations

from typing import Any

#: Every ``hello`` carries this; a mismatch is a hard error.
PROTOCOL_VERSION = 1
REQUEST_TIMEOUT_SECONDS = 60.0
PING_INTERVAL_SECONDS = 25.0
SILENT_TIMEOUT_SECONDS = 90.0
MAX_EVENT_BYTES = 64 * 1024
DELTA_FLUSH_MS = 80

#: Largest WebSocket text message the gateway will read. Session events are capped at 64 KiB
#: (§4), but a ``session.send`` may carry 8 attachments of 6 MiB each (§5); base64 inflates that
#: to roughly 64 MiB, so the read limit has to clear it or a legal frame would kill the socket.
MAX_ATTACHMENTS = 8
MAX_ATTACHMENT_BYTES = 6 * 1024 * 1024
WS_MAX_MESSAGE_BYTES = 72 * 1024 * 1024

Frame = dict[str, Any]

#: App requests the gateway forwards to a device, mapped to the field that names the target.
FORWARDED_BY_SESSION = frozenset(
    {
        "session.send",
        "session.stop",
        "session.approve",
        "session.answer",
        "session.set",
        "session.history",
        "session.block",
        "session.commands",
        "session.command",
        "session.queue_remove",
        # A35: the device owns the schedule, so both of these are questions only it can answer.
        "session.resume_set",
        "session.resume_cancel",
        "session.takeover",
        "session.archive",
        "session.delete",
    }
)
#: Forwarded requests whose reply carries the device's `Session` as `result.session` (§6.3), which
#: the gateway marks as it marks every session an app receives (A47).
SESSION_RESULT_TYPES = frozenset(
    {
        "session.create",
        "session.set",
        "session.resume_set",
        "session.resume_cancel",
        "session.takeover",
        "session.archive",
    }
)
#: A38: a terminal belongs to a machine, never to a session, so all five name the device.
#: ``terminal.detach`` is deliberately absent: only the gateway sends it, on its own account.
TERMINAL_OPEN = "terminal.open"
TERMINAL_INPUT = "terminal.input"
TERMINAL_RESIZE = "terminal.resize"
TERMINAL_ATTACH = "terminal.attach"
TERMINAL_CLOSE = "terminal.close"
TERMINAL_DETACH = "terminal.detach"
TERMINAL_OUTPUT = "terminal.output"
TERMINAL_EXITED = "terminal.exited"

FORWARDED_BY_DEVICE = frozenset(
    {
        "session.create",
        "device.dirs",
        "device.mkdir",
        "device.git",
        "device.agents",
        "device.update",
        TERMINAL_OPEN,
        TERMINAL_INPUT,
        TERMINAL_RESIZE,
        TERMINAL_ATTACH,
        TERMINAL_CLOSE,
    }
)
FORWARDED_TYPES = FORWARDED_BY_SESSION | FORWARDED_BY_DEVICE

#: A38: the two frames a device pushes for a terminal. Both name one app connection in ``to``,
#: and the gateway delivers them to that connection alone (§7.3).
TERMINAL_PUSH_TYPES = frozenset({TERMINAL_OUTPUT, TERMINAL_EXITED})

#: A22: an accepted reply to this puts the device into `updating` until it comes back.
DEVICE_UPDATE = "device.update"
DEVICE_UPDATE_FAILED = "update.failed"

#: Frames a device may send unsolicited.
DEVICE_PUSH_TYPES = (
    frozenset(
        {
            "session.updated",
            "session.removed",
            "session.event",
            "agents.updated",
            DEVICE_UPDATE_FAILED,
        }
    )
    | TERMINAL_PUSH_TYPES
)

#: Amendment A4 close codes. 4401 tells an app to stop reconnecting and return to login; 4403
#: means the credential is real but not allowed here; 4001 means a newer device connection took
#: this device's slot; 1008 is reserved for protocol violations. Anything else means "reconnect
#: with backoff".
CLOSE_UNAUTHORIZED = 4401
CLOSE_FORBIDDEN = 4403
CLOSE_DEVICE_REPLACED = 4001
CLOSE_PROTOCOL_ERROR = 1008
CLOSE_SLOW_CLIENT = 4008
#: This account already holds as many `/ws/app` sockets as the gateway allows, so its oldest was
#: closed. Deliberately outside the A4 table: §2.4 gives every other code the meaning "reconnect
#: with backoff", which is exactly right here, so this needs no amendment.
CLOSE_TOO_MANY_APPS = 4009

#: The full §1 error vocabulary lives here even where the gateway does not raise a code itself, so
#: every component reads one list rather than three partial ones.
ERROR_BAD_REQUEST = "bad_request"
ERROR_UNAUTHORIZED = "unauthorized"
ERROR_FORBIDDEN = "forbidden"
ERROR_NOT_FOUND = "not_found"
ERROR_DEVICE_OFFLINE = "device_offline"
ERROR_AGENT_UNAVAILABLE = "agent_unavailable"
ERROR_CONFLICT = "conflict"
ERROR_TIMEOUT = "timeout"
ERROR_INTERNAL = "internal"
ERROR_UNSUPPORTED = "unsupported"
ERROR_TOO_LARGE = "too_large"


def ok_reply(request_id: str, result: Frame) -> Frame:
    return {"type": "reply", "id": request_id, "ok": True, "result": result}


def error_reply(request_id: str, code: str, message: str) -> Frame:
    return {
        "type": "reply",
        "id": request_id,
        "ok": False,
        "error": {"code": code, "message": message},
    }


def frame_type(frame: Frame) -> str:
    value = frame.get("type")
    return value if isinstance(value, str) else ""


def request_id(frame: Frame) -> str:
    value = frame.get("id")
    return value if isinstance(value, str) and value else ""


def text_field(frame: Frame, name: str) -> str:
    value = frame.get(name)
    return value if isinstance(value, str) else ""


def int_field(frame: Frame, name: str) -> int | None:
    value = frame.get(name)
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def bool_field(frame: Frame, name: str) -> bool | None:
    """A boolean a frame may omit. ``None`` means the peer said nothing, which is not ``False``.

    A38 turns on this distinction: a device older than the amendment reports no ``terminal`` at
    all, and the `Device` object leaves the field out rather than claiming the shell is off.
    """
    value = frame.get(name)
    return value if isinstance(value, bool) else None


def object_field(frame: Frame, name: str) -> Frame | None:
    value = frame.get(name)
    return value if isinstance(value, dict) else None
