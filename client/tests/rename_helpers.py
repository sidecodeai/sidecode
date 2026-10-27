"""Helpers for the session rename tests (A55), shared by the device's test modules.

An agent's tests call its plugin's `rename` hook through `rename_spec`, or the
whole pipeline through `hub.set_options`; a hub test installs a probe in place
of the hook with `install_rename`.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from rc_client.agents import registry
from rc_client.agents.registry import RenameHook, RenameSpec
from rc_client.errors import RcError
from rc_client.sessions import rename
from rc_client.sessions.hub import SessionEntry, SessionHub


def rename_spec(hub: SessionHub, entry: SessionEntry, name: str) -> RenameSpec:
    """The spec the hub hands a plugin's hook, for a test that calls the hook itself."""
    return rename.spec_for(hub, entry, name)


def install_rename(monkeypatch: pytest.MonkeyPatch, agent: str, hook: RenameHook) -> None:
    """Make `hook` the agent's `rename` hook for one test, whether or not it has one."""
    monkeypatch.setattr(registry.plugin(agent), "rename", hook, raising=False)


def remove_rename(monkeypatch: pytest.MonkeyPatch, agent: str) -> None:
    """Take the agent's `rename` hook away for one test, so it cannot be renamed."""
    monkeypatch.delattr(registry.plugin(agent), "rename", raising=False)


class RenameProbe:
    """A `rename` hook that records how it was called and answers as it is told.

    `held` is the name the agent says it now holds (the requested one by
    default), `error` is a refusal to raise instead, and `gate`, when set, is
    waited on before the hook answers, so a test can hold a rename open.
    """

    def __init__(self, held: str | None = None, error: RcError | None = None) -> None:
        self.held = held
        self.error = error
        self.gate: asyncio.Event | None = None
        self.names: list[str] = []
        self.locked: list[bool] = []

    async def __call__(self, spec: RenameSpec) -> str:
        self.names.append(spec.name)
        self.locked.append(spec.entry.lock.locked())
        if self.gate is not None:
            await self.gate.wait()
        if self.error is not None:
            raise self.error
        return spec.name if self.held is None else self.held


def summaries(frames: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every session summary the device published, in order."""
    return [frame["session"] for frame in frames if frame.get("type") == "session.updated"]


def title_events(frames: list[dict[str, Any]]) -> list[str]:
    """The titles the apps were told about through `meta` events, in order."""
    return [
        frame["event"]["title"]
        for frame in frames
        if frame.get("type") == "session.event"
        and frame["event"]["kind"] == "meta"
        and "title" in frame["event"]
    ]
