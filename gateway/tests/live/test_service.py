"""The call registry: who is let in, the day's time, the cooldown, and stopping the gateway."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any, cast

import pytest
from fastapi import WebSocket

from rc_gateway.live.budget import DailyBudget
from rc_gateway.live.config import LiveConfig
from rc_gateway.live.errors import LiveError
from rc_gateway.live.meter import NoMeter
from rc_gateway.live.policy import Admission, AllowlistPolicy, LivePolicy, LiveStatus
from rc_gateway.live.service import LiveService
from rc_gateway.live.upstream import UpstreamLink
from rc_gateway.security import Credential
from rc_gateway.state import GatewayState

from .fakes import API_KEY, FakeClock, FakeUpstream, until

pytestmark = pytest.mark.timeout(20)

#: A fixed moment in the middle of a UTC day, so the day's count cannot turn over mid-test.
MIDDAY = 1_800_000_000.0


class FakeSocket:
    """What the browser link needs of a WebSocket, with the browser's frames queued by hand."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.closed: int | None = None
        self.gone = False
        self._inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    async def send_text(self, text: str) -> None:
        if self.gone:
            raise RuntimeError("the browser has gone")
        self.sent.append(json.loads(text))

    async def send_bytes(self, data: bytes) -> None:
        pass

    async def receive(self) -> dict[str, Any]:
        return await self._inbox.get()

    async def close(self, code: int = 1000, reason: str | None = None) -> None:
        self.closed = code

    def command(self, **message: Any) -> None:
        self._inbox.put_nowait({"type": "websocket.receive", "text": json.dumps(message)})

    def leave(self) -> None:
        """The browser closes its end without a word."""
        self.gone = True
        self._inbox.put_nowait({"type": "websocket.disconnect"})

    def types(self) -> list[str]:
        return [str(message.get("type")) for message in self.sent]


class HangingCloseUpstream(FakeUpstream):
    """A Live API socket whose close never finishes until the test lets it."""

    def __init__(self) -> None:
        super().__init__()
        self.release = asyncio.Event()

    async def close(self) -> None:
        await self.release.wait()
        await super().close()


class Connector:
    """Dials nothing: every call gets an in-memory Live API (or the one it was given)."""

    def __init__(self, upstream: FakeUpstream | None = None) -> None:
        self.dialled = 0
        self._fixed = upstream

    async def __call__(
        self, config: LiveConfig, session: dict[str, Any]
    ) -> tuple[UpstreamLink, dict[str, Any]]:
        self.dialled += 1
        upstream = self._fixed if self._fixed is not None else FakeUpstream()
        return upstream, {"session": {"id": f"live_{self.dialled}"}}


class SlowConnector(Connector):
    """A Live API that takes its time to answer, so a browser can leave in the meantime."""

    def __init__(self, upstream: FakeUpstream) -> None:
        super().__init__(upstream)
        self.answer = asyncio.Event()

    async def __call__(
        self, config: LiveConfig, session: dict[str, Any]
    ) -> tuple[UpstreamLink, dict[str, Any]]:
        await self.answer.wait()
        return await super().__call__(config, session)


class Unreachable(Connector):
    """A Live API that cannot be reached."""

    async def __call__(
        self, config: LiveConfig, session: dict[str, Any]
    ) -> tuple[UpstreamLink, dict[str, Any]]:
        raise LiveError("upstream_unavailable", "no route")


def fake_state() -> GatewayState:
    async def revoked_event(claims: Any) -> asyncio.Event:
        return asyncio.Event()

    return cast(
        GatewayState, SimpleNamespace(sessions=SimpleNamespace(revoked_event=revoked_event))
    )


def credential(username: str) -> Credential:
    claims = SimpleNamespace(username=username)
    return Credential(cast(Any, claims), "member", from_cookie=False)


def make_service(
    connector: Connector | None = None,
    *,
    clock: Callable[[], float] = time.monotonic,
    budget: DailyBudget | None = None,
    policy: LivePolicy | None = None,
    close_timeout: float = 8.0,
    **settings: Any,
) -> LiveService:
    values: dict[str, Any] = {"cooldown_seconds": 0.0, **settings}
    config = LiveConfig(api_key=API_KEY, allowed_users=frozenset({"admin", "dana"}), **values)
    return LiveService(
        fake_state(),
        config,
        connector if connector is not None else Connector(),
        clock=clock,
        policy=policy if policy is not None else AllowlistPolicy(config, budget),
        close_timeout=close_timeout,
    )


def start_call(
    service: LiveService, username: str = "admin"
) -> tuple[FakeSocket, asyncio.Task[None]]:
    socket = FakeSocket()
    run = asyncio.create_task(service.run_call(cast(WebSocket, socket), credential(username)))
    return socket, run


async def place_call(service: LiveService, username: str = "admin") -> FakeSocket:
    """One call, hung up as soon as it is open (or refused): what its socket saw."""
    socket, run = start_call(service, username)
    await until(lambda: bool(socket.sent))
    if socket.types()[0] == "ready":
        socket.command(type="hangup")
    await asyncio.wait_for(run, 5)
    return socket


# ---- the day's time ----


async def test_a_call_is_charged_to_the_days_time_when_it_ends() -> None:
    clock = FakeClock()
    budget = DailyBudget(7200, wall_clock=FakeClock(MIDDAY))
    service = make_service(clock=clock, budget=budget)
    socket, run = start_call(service)
    await until(lambda: socket.types() == ["ready"])
    clock.advance(100)
    socket.command(type="hangup")
    await asyncio.wait_for(run, 5)
    assert budget.remaining("admin") == 7100
    assert budget.remaining("dana") == 7200


async def test_a_call_is_given_what_is_left_of_the_day_and_no_more() -> None:
    budget = DailyBudget(600, wall_clock=FakeClock(MIDDAY))
    budget.charge("admin", 570)
    service = make_service(clock=FakeClock(), budget=budget)
    socket, run = start_call(service)
    await until(lambda: socket.types() == ["ready"])
    assert socket.sent[0]["seconds_left"] == 30
    socket.command(type="hangup")
    await asyncio.wait_for(run, 5)


async def test_a_day_used_up_refuses_the_call_before_the_service_is_dialled() -> None:
    budget = DailyBudget(600, wall_clock=FakeClock(MIDDAY))
    budget.charge("admin", 600)
    connector = Connector()
    service = make_service(connector, budget=budget)
    socket = await place_call(service)
    assert socket.sent == [{"type": "ended", "reason": "daily_limit"}]
    assert socket.closed == 4429
    assert connector.dialled == 0
    # Another account's day is its own.
    other = await place_call(service, "dana")
    assert other.types()[0] == "ready"


async def test_a_refused_start_charges_nothing_and_starts_no_cooldown() -> None:
    budget = DailyBudget(600, wall_clock=FakeClock(MIDDAY))
    service = make_service(Unreachable(), budget=budget, cooldown_seconds=60.0)
    first = await place_call(service)
    second = await place_call(service)
    assert first.sent == second.sent == [{"type": "ended", "reason": "upstream_unavailable"}]
    assert budget.remaining("admin") == 600


# ---- a browser that leaves while the Live API is being dialled ----


async def test_a_browser_that_leaves_while_the_service_is_dialled_leaves_no_session_open() -> None:
    upstream = FakeUpstream()
    connector = SlowConnector(upstream)
    service = make_service(connector)
    socket, run = start_call(service)
    await asyncio.sleep(0.05)  # the call is now waiting for the Live API to answer
    socket.leave()
    connector.answer.set()
    # Nobody is reading the Live API yet, so there is no final word to wait for: it closes at once.
    await asyncio.wait_for(run, 2)

    assert upstream.closed
    assert "session.close" in upstream.types()
    assert (await place_call(service)).types()[0] == "ready"


# ---- the cooldown and the slot ----


async def test_the_cooldown_is_counted_from_the_end_of_the_call() -> None:
    clock = FakeClock()
    service = make_service(clock=clock, cooldown_seconds=10.0)
    assert (await place_call(service)).types()[0] == "ready"
    clock.advance(9)
    refused = await place_call(service)
    assert refused.sent == [{"type": "ended", "reason": "cooldown"}]
    assert refused.closed == 4429
    clock.advance(2)
    assert (await place_call(service)).types()[0] == "ready"


async def test_a_second_call_on_one_account_is_refused_while_the_first_is_open() -> None:
    service = make_service()
    first, run = start_call(service)
    await until(lambda: first.types() == ["ready"])
    second = await place_call(service)
    assert second.sent == [{"type": "ended", "reason": "call_active"}]
    assert second.closed == 4409
    first.command(type="hangup")
    await asyncio.wait_for(run, 5)


async def test_an_account_that_is_not_listed_is_refused() -> None:
    connector = Connector()
    service = make_service(connector)
    refused = await place_call(service, "erin")
    assert refused.sent == [{"type": "ended", "reason": "not_allowed"}]
    assert refused.closed == 4403
    assert connector.dialled == 0


# ---- a policy of its own ----


class ScriptedPolicy:
    """A gateway's own policy, as a test writes it: who is refused and why, and the day's cap."""

    def __init__(self, *, refuse: dict[str, Exception] | None = None) -> None:
        self.refuse = refuse or {}
        self.asked: list[str] = []
        self.meters: list[NoMeter] = []
        self.budget_seconds: float | None = None
        self.hold: asyncio.Event | None = None

    async def status(self, username: str) -> LiveStatus:
        error = self.refuse.get(username)
        reason = error.code if isinstance(error, LiveError) else None
        return LiveStatus(enabled=True, available=error is None, reason=reason)

    async def admit(self, username: str) -> Admission:
        self.asked.append(username)
        if self.hold is not None and username == "admin":
            await self.hold.wait()
        if username in self.refuse:
            raise self.refuse[username]
        meter = NoMeter()
        self.meters.append(meter)
        return Admission(meter, budget_seconds=self.budget_seconds)


@pytest.mark.parametrize(
    ("reason", "code"),
    [("subscription_required", 4402), ("quota_exceeded", 4402), ("billing_not_configured", 4503)],
)
async def test_a_refusal_from_the_policy_is_the_reason_and_nothing_is_dialled(
    reason: str, code: int
) -> None:
    connector = Connector()
    policy = ScriptedPolicy(refuse={"admin": LiveError(reason)})
    service = make_service(connector, policy=policy)
    refused = await place_call(service)
    assert refused.sent == [{"type": "ended", "reason": reason}]
    assert refused.closed == code
    assert connector.dialled == 0
    # Another account is another question.
    assert (await place_call(service, "dana")).types()[0] == "ready"


async def test_a_policy_that_fails_unexpectedly_is_an_internal_error_and_holds_no_slot() -> None:
    policy = ScriptedPolicy(refuse={"admin": RuntimeError("the billing service fell over")})
    connector = Connector()
    service = make_service(connector, policy=policy, max_concurrent_calls=1)
    refused = await place_call(service)
    assert refused.sent == [{"type": "ended", "reason": "internal_error"}]
    assert refused.closed == 1011
    assert connector.dialled == 0
    assert (await place_call(service, "dana")).types()[0] == "ready"


async def test_the_meter_a_policy_admits_is_the_meter_of_the_call() -> None:
    policy = ScriptedPolicy()
    service = make_service(policy=policy)
    await place_call(service)
    assert policy.asked == ["admin"]
    assert len(policy.meters) == 1


async def test_a_policy_with_no_cap_leaves_the_call_its_whole_length() -> None:
    policy = ScriptedPolicy()
    service = make_service(policy=policy, clock=FakeClock(), max_call_seconds=900.0)
    socket, run = start_call(service)
    await until(lambda: socket.types() == ["ready"])
    assert socket.sent[0]["seconds_left"] == 900
    socket.command(type="hangup")
    await asyncio.wait_for(run, 5)


async def test_a_policy_cap_bounds_the_call() -> None:
    policy = ScriptedPolicy()
    policy.budget_seconds = 45.0
    service = make_service(policy=policy, clock=FakeClock())
    socket, run = start_call(service)
    await until(lambda: socket.types() == ["ready"])
    assert socket.sent[0]["seconds_left"] == 45
    socket.command(type="hangup")
    await asyncio.wait_for(run, 5)


async def test_a_slow_admission_does_not_hold_up_another_accounts_call() -> None:
    policy = ScriptedPolicy()
    policy.hold = asyncio.Event()
    service = make_service(policy=policy)
    slow, slow_run = start_call(service, "admin")
    await until(lambda: policy.asked == ["admin"])
    other = await place_call(service, "dana")
    assert other.types()[0] == "ready"
    assert slow.sent == []
    policy.hold.set()
    await until(lambda: slow.types() == ["ready"])
    slow.command(type="hangup")
    await asyncio.wait_for(slow_run, 5)


def test_a_gateway_that_bills_its_calls_cannot_build_the_service_without_a_policy() -> None:
    config = LiveConfig(api_key=API_KEY)
    billing = cast(GatewayState, SimpleNamespace(paid=object()))
    with pytest.raises(ValueError, match="needs a policy"):
        LiveService(billing, config, Connector())
    # With a policy of its own it is built, and a gateway that bills nothing needs none.
    assert LiveService(billing, config, Connector(), policy=AllowlistPolicy(config))
    assert LiveService(fake_state(), config, Connector())


async def test_the_status_is_the_policys() -> None:
    policy = ScriptedPolicy(refuse={"admin": LiveError("subscription_required")})
    service = make_service(policy=policy)
    assert await service.status("admin") == {
        "enabled": True,
        "available": False,
        "reason": "subscription_required",
    }
    assert await service.status("dana") == {"enabled": True, "available": True, "reason": None}


# ---- stopping the gateway ----


async def test_stopping_the_service_does_not_wait_for_a_call_that_cannot_close() -> None:
    upstream = HangingCloseUpstream()
    service = make_service(Connector(upstream), close_timeout=0.2)
    socket, run = start_call(service)
    await until(lambda: socket.types() == ["ready"])

    started = time.monotonic()
    await service.close()
    assert time.monotonic() - started < 2.0

    # The call is still closing the Live API's socket in the background, and finishes once it can.
    assert not run.done()
    assert socket.sent[-1] == {"type": "ended", "reason": "gateway_stopping"}
    upstream.release.set()
    await asyncio.wait_for(run, 5)
    assert upstream.closed
    assert "session.close" in upstream.types()


async def test_stopping_the_service_with_no_calls_returns_at_once() -> None:
    await asyncio.wait_for(make_service().close(), 1)
