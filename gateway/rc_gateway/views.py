"""Shared object projections (protocol §3).

One place builds the ``Device`` object so the HTTP list, the app ``hello`` and every
``device.updated`` push agree field for field. The agent inventory is read from the persisted
device record, which the hub refreshes on every hello and ``agents.updated``, so the list renders
the same whether the device is online or asleep.

The same applies to ``User`` (§4.10) and to the ``UserRecord`` the admin lists: one projection for
login, ``GET /api/session``, the app ``hello`` and the account routes.
"""

from __future__ import annotations

from typing import Any

from .devices import DeviceRecord
from .users import UserRecord

Device = dict[str, Any]
User = dict[str, Any]


def user_view(record: UserRecord) -> User:
    """The ``User`` of §4.10: who is signed in, and what §3.9 is gated on."""
    return {"username": record.username, "role": record.role}


def user_record_view(record: UserRecord, devices: int) -> User:
    """The ``UserRecord`` of §4.10, as only an admin sees it. The hash is never part of it."""
    return {
        "username": record.username,
        "role": record.role,
        "state": record.state,
        "created_at": record.created_at * 1000,
        "last_login_at": None if record.last_login_at is None else record.last_login_at * 1000,
        "devices": devices,
    }


def device_view(
    record: DeviceRecord,
    *,
    online: bool,
    latency_ms: int | None = None,
    last_seen: int | None = None,
) -> Device:
    view: Device = {
        "device_id": record.device_id,
        "name": record.name,
        "platform": record.platform,
        "hostname": record.hostname,
        "arch": record.arch,
        "client_version": record.client_version,
        "online": online,
        "last_seen": last_seen if last_seen is not None else _millis(record.last_seen),
        "created_at": record.created_at * 1000,
        "latency_ms": latency_ms,
        "agents": record.agents,
        # A22. `update_state` is always present, including its `idle` default, so an app reads one
        # field rather than an absence.
        "client_build": record.client_build,
        "update_state": record.update_state,
        "update_message": record.update_message,
    }
    # A38, and unlike the A22 three: the field is left out for a client that never said, which is
    # what "older than the amendment" looks like and is not the same as a device with no shell.
    if record.terminal is not None:
        view["terminal"] = record.terminal
    return view


def has_available_agent(agents: list[dict[str, Any]] | None) -> bool:
    return any(isinstance(agent, dict) and agent.get("available") for agent in agents or [])


def _millis(seconds: int | None) -> int | None:
    return None if seconds is None else seconds * 1000
