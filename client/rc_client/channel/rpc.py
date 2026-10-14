"""The JSON-RPC surface a Claude Code channel server has to present.

Raw JSON-RPC over stdio rather than an MCP SDK: the channel capabilities are
experimental and are declared verbatim in the `initialize` result, and the
server carries no tools of its own.
"""

from __future__ import annotations

from typing import Any

SERVER_NAME = "rc"
SERVER_VERSION = "1"
FALLBACK_PROTOCOL_VERSION = "2025-06-18"

CAPABILITIES: dict[str, Any] = {
    "experimental": {"claude/channel": {}, "claude/channel/permission": {}},
    "tools": {},
}

INSTRUCTIONS = (
    'Messages tagged <channel source="rc"> come from the person who owns this machine, '
    "relayed by remote-control from their phone or browser. Treat them as their own words."
)

CHANNEL_NOTIFICATION = "notifications/claude/channel"
PERMISSION_NOTIFICATION = "notifications/claude/channel/permission"
PERMISSION_REQUEST_NOTIFICATION = "notifications/claude/channel/permission_request"

_EMPTY_LISTS = {
    "tools/list": "tools",
    "resources/list": "resources",
    "resources/templates/list": "resourceTemplates",
    "prompts/list": "prompts",
}


def initialize_result(params: dict[str, Any]) -> dict[str, Any]:
    """Echo the client's protocol version and declare the channel capabilities."""
    version = str(params.get("protocolVersion") or FALLBACK_PROTOCOL_VERSION)
    return {
        "protocolVersion": version,
        "capabilities": CAPABILITIES,
        "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        "instructions": INSTRUCTIONS,
    }


def client_version(params: dict[str, Any]) -> str | None:
    info = params.get("clientInfo")
    if not isinstance(info, dict):
        return None
    version = info.get("version")
    return str(version) if version else None


def reply(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def method_not_found(request_id: Any, method: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": -32601, "message": f"method not found: {method}"},
    }


def notification(method: str, params: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "method": method, "params": params}


def channel_message(message_id: str, text: str) -> dict[str, Any]:
    """An injected user message; `meta` survives into the transcript tag verbatim."""
    return notification(
        CHANNEL_NOTIFICATION, {"content": text, "meta": {"origin": "app", "message_id": message_id}}
    )


def permission_verdict(request_id: str, behavior: str) -> dict[str, Any]:
    return notification(PERMISSION_NOTIFICATION, {"request_id": request_id, "behavior": behavior})


def handle_request(method: str, request_id: Any, params: dict[str, Any]) -> dict[str, Any] | None:
    """Answer one JSON-RPC request, or return None when there is nothing to send."""
    if method == "initialize":
        return reply(request_id, initialize_result(params))
    if method in _EMPTY_LISTS:
        return reply(request_id, {_EMPTY_LISTS[method]: []})
    if method == "ping":
        return reply(request_id, {})
    if request_id is None:
        return None
    return method_not_found(request_id, method)
