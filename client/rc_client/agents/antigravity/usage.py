"""What is left of the account's Antigravity quota, from `agy`'s own usage query (A64, 4.2).

`agy -p "/usage" --output-format json` starts no turn and keeps no conversation: it prints each
model group with its five-hour and weekly windows, each the fraction left and when it fully resets.
The device runs it only for a `device.agents` reply and to confirm a limit stop (7.2), never on a
timer, in the empty folder it lists from. An answer is held a minute, so apps asking together cost
one run, and the last good one stands in for up to half an hour when a run fails.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from ...child_env import sanitized_child_env
from ...models import AgentAccount, AgentLimit, now_ms
from ..earlier_reading import standing_in
from . import runtime

# It answers in two to four seconds, more on a cold start; a `device.agents` reply waits eight.
USAGE_TIMEOUT = 7.0
HOLD = 60.0
PROVIDER = "google"
WINDOWS = {"5h": 300, "weekly": 10080}
GROUP_MODELS = "Models within this group:"
SLOW = "agy did not answer its usage query in time"
FAILED = "agy's usage query failed"
UNREADABLE = "agy's usage query answered in a form the device does not read"


class UsageFailed(Exception):
    """Why a usage query has no answer, in the device's words."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class Bucket:
    minutes: int
    remaining: float
    resets_at: int | None


@dataclass(frozen=True, slots=True)
class Group:
    name: str
    models: tuple[str, ...]
    buckets: tuple[Bucket, ...]


@dataclass(frozen=True, slots=True)
class Reading:
    groups: tuple[Group, ...]
    checked_at: int


def parse(data: Any) -> tuple[Group, ...]:
    """The groups of one answer, in `agy`'s order, each with its five-hour window first."""
    command = data.get("command") if isinstance(data, dict) else None
    body = command.get("data") if isinstance(command, dict) else None
    groups = body.get("groups") if isinstance(body, dict) else None
    if not isinstance(groups, list):
        raise UsageFailed(UNREADABLE)
    found: list[Group] = []
    for group in groups:
        if not isinstance(group, dict):
            continue
        buckets = [bucket for bucket in map(_bucket, group.get("buckets") or []) if bucket]
        buckets.sort(key=lambda bucket: bucket.minutes)
        found.append(
            Group(
                name=str(group.get("name") or ""),
                models=_models(str(group.get("description") or "")),
                buckets=tuple(buckets),
            )
        )
    return tuple(found)


def _bucket(raw: Any) -> Bucket | None:
    if not isinstance(raw, dict):
        return None
    minutes = WINDOWS.get(str(raw.get("window") or ""))
    remaining = raw.get("remaining_fraction")
    if minutes is None or isinstance(remaining, bool) or not isinstance(remaining, int | float):
        return None
    fraction = min(1.0, max(0.0, float(remaining)))
    # A window none of which is used has no reset of its own: `agy` names the query's time plus
    # the window, which moves on every query.
    resets = _instant(raw.get("reset_time")) if fraction < 1.0 else None
    return Bucket(minutes, fraction, resets)


def _models(description: str) -> tuple[str, ...]:
    if not description.startswith(GROUP_MODELS):
        return ()
    listed = description[len(GROUP_MODELS) :]
    return tuple(name.strip() for name in listed.split(",") if name.strip())


def _instant(value: Any) -> int | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return int(parsed.timestamp() * 1000)


def limits(reading: Reading) -> list[AgentLimit]:
    """Each window as an `AgentLimit`, scoped to its group in `agy`'s words."""
    return [
        AgentLimit(
            window_minutes=bucket.minutes,
            used_percent=round((1.0 - bucket.remaining) * 100, 2),
            resets_at=bucket.resets_at,
            scope=group.name or None,
        )
        for group in reading.groups
        for bucket in group.buckets
    ]


async def query(binary: str) -> tuple[Group, ...]:
    """Run the query once, in the device's own empty folder."""
    try:
        process = await asyncio.create_subprocess_exec(
            binary,
            "-p",
            "/usage",
            "--output-format",
            "json",
            cwd=str(runtime.lists_dir()),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=sanitized_child_env(),
        )
    except OSError as exc:
        raise UsageFailed(FAILED) from exc
    try:
        out, _ = await asyncio.wait_for(process.communicate(), timeout=USAGE_TIMEOUT)
    except TimeoutError as exc:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
            await process.wait()
        raise UsageFailed(SLOW) from exc
    if process.returncode != 0:
        raise UsageFailed(FAILED)
    try:
        data = json.loads(out)
    except json.JSONDecodeError as exc:
        raise UsageFailed(UNREADABLE) from exc
    return parse(data)


class Usage:
    """The device's readings of the quota: held a minute, stood in for thirty, never timed."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._good: Reading | None = None
        self._good_at = 0.0
        self._flight: asyncio.Future[Reading] | None = None

    async def read(self, binary: str, *, fresh: bool = False) -> Reading:
        """A reading; one under a minute old answers again unless `fresh` asks for a new run."""
        good = self._good
        if not fresh and good is not None and self._clock() - self._good_at < HOLD:
            return good
        flight = self._flight
        if flight is None or flight.done():
            flight = asyncio.ensure_future(self._run(binary))
            self._flight = flight
        return await asyncio.shield(flight)

    async def _run(self, binary: str) -> Reading:
        reading = Reading(await query(binary), now_ms())
        self._good, self._good_at = reading, self._clock()
        return reading

    async def account(self, binary: str) -> AgentAccount:
        """The Google account as a `device.agents` reply carries it, windows and all."""
        account = AgentAccount(provider=PROVIDER, method="account")
        try:
            reading = await self.read(binary)
        except UsageFailed as failed:
            now = now_ms()
            earlier = self._good
            windows = standing_in(limits(earlier), earlier.checked_at, now) if earlier else []
            if windows and earlier is not None:
                account.limits, account.limits_checked_at = windows, earlier.checked_at
            else:
                account.limits_error, account.limits_checked_at = failed.reason, now
            return account
        account.limits, account.limits_checked_at = limits(reading), reading.checked_at
        return account
