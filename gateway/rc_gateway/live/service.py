"""The call registry and its limits: how many calls at once, and when again.

Who may call, and what a call costs, is the policy's (``policy.py``).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Any

from fastapi import WebSocket

from ..logging import logger
from ..ratelimit import RateLimiter
from ..rejects import RejectionLog
from ..security import Credential
from ..state import GatewayState
from .access import AccountAccess
from .browser import WebSocketLink
from .call import LiveCall
from .config import LiveConfig
from .drafts import DraftBox
from .errors import LiveError
from .gate import ConfirmGate
from .policy import Admission, AllowlistPolicy, LivePolicy
from .prompts import session_config
from .tools import ToolRunner
from .upstream import UpstreamConnector, UpstreamLink

log = logger("rc_gateway.live.service")

#: Calls one address may start in a minute; each one opens a billable session.
UPGRADES_PER_MINUTE = 10
#: How long stopping the gateway waits for calls to close before the hub and the index shut down.
CLOSE_TIMEOUT_SECONDS = 8.0


class LiveService:
    def __init__(
        self,
        state: GatewayState,
        config: LiveConfig,
        connector: UpstreamConnector,
        clock: Callable[[], float] = time.monotonic,
        policy: LivePolicy | None = None,
        close_timeout: float = CLOSE_TIMEOUT_SECONDS,
    ) -> None:
        if policy is None and getattr(state, "paid", None) is not None:
            raise ValueError("a gateway that bills its calls needs a policy that bills them")
        self._state = state
        self.config = config
        self._connector = connector
        self._clock = clock
        self._policy = policy if policy is not None else AllowlistPolicy(config)
        self._close_timeout = close_timeout
        self.rejects = RejectionLog()
        self.upgrade_limiter = RateLimiter(max_per_ip=UPGRADES_PER_MINUTE)
        self._calls: dict[str, LiveCall] = {}
        self._starting: set[str] = set()
        self._cooldown_until: dict[str, float] = {}
        self._lock = asyncio.Lock()

    async def status(self, username: str) -> dict[str, Any]:
        return (await self._policy.status(username)).view()

    async def run_call(self, ws: WebSocket, credential: Credential) -> None:
        """Serve one accepted browser socket: refuse it, or run a call on it to the end."""
        username = credential.username
        browser = WebSocketLink(ws)
        try:
            # Who may call comes first, and before a slot is held: it can take a round trip.
            admission = await self._policy.admit(username)
            await self._reserve(username)
        except Exception as exc:
            await self._refuse(browser, exc)
            return
        try:
            call = await self._open(username, browser, admission)
        except Exception as exc:
            await self._refuse(browser, exc)
            return
        finally:
            self._starting.discard(username)
        watcher = asyncio.create_task(self._watch_login(call, credential))
        try:
            await call.run()
        finally:
            watcher.cancel()

    @staticmethod
    async def _refuse(browser: WebSocketLink, error: Exception) -> None:
        """Tell the browser why its call did not start: the reason, or an internal error."""
        if isinstance(error, LiveError):
            await browser.end(error.code)
            return
        log.warning("live call could not start", error=type(error).__name__)
        await browser.end("internal_error")

    async def _reserve(self, username: str) -> None:
        async with self._lock:
            if username in self._calls or username in self._starting:
                raise LiveError("call_active", "a call is already open for this account")
            if self._clock() < self._cooldown_until.get(username, 0.0):
                raise LiveError("cooldown", "wait a moment before calling again")
            if len(self._calls) + len(self._starting) >= self.config.max_concurrent_calls:
                raise LiveError("too_busy", "too many calls are open on this gateway")
            self._starting.add(username)

    async def _open(self, username: str, browser: WebSocketLink, admission: Admission) -> LiveCall:
        upstream, started = await self._connector(self.config, session_config(self.config))
        try:
            call = self._build(username, browser, upstream, started, admission)
        except BaseException:
            await upstream.close()
            raise
        self._calls[username] = call
        return call

    def _build(
        self,
        username: str,
        browser: WebSocketLink,
        upstream: UpstreamLink,
        started: dict[str, Any],
        admission: Admission,
    ) -> LiveCall:
        drafts, gate = DraftBox(), ConfirmGate()
        tools = ToolRunner(AccountAccess(self._state, username), drafts, gate)
        return LiveCall(
            username=username,
            browser=browser,
            upstream=upstream,
            started=started,
            config=self.config,
            tools=tools,
            drafts=drafts,
            gate=gate,
            on_end=self._ended,
            meter=admission.meter,
            budget_seconds=admission.budget_seconds,
            clock=self._clock,
        )

    async def _ended(self, call: LiveCall) -> None:
        """The slot and the cooldown, settled as soon as the call is over."""
        if self._calls.get(call.username) is call:
            del self._calls[call.username]
            self._cooldown_until[call.username] = self._clock() + self.config.cooldown_seconds

    async def _watch_login(self, call: LiveCall, credential: Credential) -> None:
        """End the call the moment its login is signed out, as the other sockets do (A24)."""
        event = await self._state.sessions.revoked_event(credential.claims)
        if event is None:
            call.request_finish("session_revoked")
            return
        await event.wait()
        call.request_finish("session_revoked")

    async def close(self) -> None:
        """End every call; the app lifespan runs this before the hub stops.

        Bounded: a call that cannot finish in time is left to finish on its own, and the hub and
        the index shut down regardless.
        """
        calls = list(self._calls.values())
        if not calls:
            return
        closing = asyncio.gather(
            *(call.finish("gateway_stopping") for call in calls), return_exceptions=True
        )
        try:
            await asyncio.wait_for(closing, self._close_timeout)
        except TimeoutError:
            log.warning("live calls still closing at shutdown", calls=len(calls))
