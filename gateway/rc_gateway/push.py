"""Notification triggers and delivery.

The gateway pushes on four observed session transitions, and on three of the things a device does
about a session the vendor's usage limit stopped (A35), and never on the content of a turn: the
payload carries identifiers and one generic sentence, so a notification on a lock screen tells the
user which device wants attention and nothing about the code being written. Either cue is
delivered to the registrations of the account that owns the device and to nobody else (A24).

Every payload also carries the account's count of sessions marked `unseen` (A47), which an iPhone
shows as its app icon's badge, and a change to that count no push carried reaches the account's
phones as a badge-only notification once the count settles (see `badge.py`).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Protocol

from .apns import PRIORITY_IMMEDIATE, PRIORITY_POWER_AWARE, ApnsProvider, ApnsResponse
from .badge import BadgeChange, BadgeSettler, Sleep
from .hub import SessionResumeNotice, SessionTransition
from .logging import logger
from .push_store import PushStore, WebPushSubscription

log = logger("rc_gateway.push")

KIND_NEEDS_APPROVAL = "needs_approval"
KIND_NEEDS_INPUT = "needs_input"
KIND_TURN_COMPLETED = "turn_completed"
KIND_ERROR = "error"
#: A35: what a device did about a session the vendor's usage limit stopped (§3.7, §7.2).
KIND_LIMIT_REACHED = "limit_reached"
KIND_RESUMED = "resumed"
KIND_RESUME_DROPPED = "resume_dropped"
#: A47: changes the app icon's count and shows nothing.
KIND_BADGE = "badge"

ACTIVE_STATES = frozenset({"running", "needs_approval", "needs_input"})
_TEXTS = {
    KIND_NEEDS_APPROVAL: "approval needed",
    KIND_NEEDS_INPUT: "waiting for your answer",
    KIND_TURN_COMPLETED: "turn finished",
    KIND_ERROR: "session error",
    KIND_LIMIT_REACHED: "paused by the usage limit",
    KIND_RESUMED: "resumed after the limit reset",
    KIND_RESUME_DROPPED: "not resumed",
}
#: The `resume` statuses worth interrupting someone about (A35). `rescheduled` is a time moving
#: and `cancelled` is something the person just did, so neither is announced.
_RESUME_KINDS = {
    "scheduled": KIND_LIMIT_REACHED,
    "fired": KIND_RESUMED,
    "dropped": KIND_RESUME_DROPPED,
}
RETRY_BACKOFF_SECONDS = (30.0, 120.0, 600.0)
MAX_ATTEMPTS = 4
WORKER_INTERVAL_SECONDS = 15.0
#: `pywebpush` is a blocking `requests` call whose own default never applies: its signature
#: defaults `timeout=None` and passes the key through, so `requests` waits until the OS gives up.
#: Ten seconds, the same as the APNs client.
WEB_PUSH_TIMEOUT_SECONDS = 10.0
#: Push delivery gets its own threads. The default executor is shared by every blocking call in
#: the gateway — 55 of them across the stores — and it holds `min(32, cpu_count + 4)` threads, six
#: on two CPUs. A browser vendor that black-holes connections must not be able to take the
#: pool that logins, device lookups and session reads are queued on.
PUSH_THREADS = 4

DeviceNameLookup = Callable[[str], Awaitable[str]]
#: A47: an account's unarchived sessions carrying `unseen`, read when a push is built.
BadgeCount = Callable[[str], Awaitable[int]]


class WebPushSender(Protocol):
    async def __call__(self, subscription: WebPushSubscription, payload: str) -> int | None: ...


def transition_kind(previous_state: str, state: str) -> str | None:
    """Map a session state change to a notification kind, or ``None`` for a silent change."""
    if state == previous_state:
        return None
    if state == "needs_approval":
        return KIND_NEEDS_APPROVAL
    if state == "needs_input":
        return KIND_NEEDS_INPUT
    if state == "error":
        return KIND_ERROR
    if state == "idle" and previous_state in ACTIVE_STATES:
        return KIND_TURN_COMPLETED
    return None


def resume_kind(status: str) -> str | None:
    """Map a ``resume`` event's status to a notification kind, or ``None`` for a silent one."""
    return _RESUME_KINDS.get(status)


def build_payload(
    kind: str, session: dict[str, Any], device_name: str, badge: int
) -> dict[str, Any]:
    """Protocol §3.7 payload: identifiers, one generic sentence and the count, never turn content.

    ``badge`` is the owner's count once the change behind this push is made (A47).
    """
    return {
        "rc": {
            "v": 1,
            "kind": kind,
            "device_id": str(session.get("device_id") or ""),
            "session_id": str(session.get("session_id") or ""),
            "device_name": device_name,
            # A badge-only push shows nothing, so it has nothing to say.
            "title": "" if kind == KIND_BADGE else f"{device_name}: {_TEXTS.get(kind, 'update')}",
            "badge": badge,
        }
    }


class PushService:
    def __init__(
        self,
        store: PushStore,
        *,
        device_name: DeviceNameLookup,
        badge_count: BadgeCount,
        vapid_private_key: str = "",
        vapid_contact: str = "",
        web_sender: WebPushSender | None = None,
        apns: ApnsProvider | None = None,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self.store = store
        self.apns = apns
        self._device_name = device_name
        self._badge_count = badge_count
        self._vapid_private_key = vapid_private_key
        self._vapid_contact = vapid_contact
        self._web_sender = web_sender or self._send_with_pywebpush
        self._worker: asyncio.Task[None] | None = None
        self._threads: ThreadPoolExecutor | None = None
        # A47. `sleep` waits out each account's quiet period; a test passes a clock it moves.
        self._badges = BadgeSettler(self.push_badge, sleep=sleep)
        #: The count each account's phones were last sent, so a count that settles where the last
        #: push left it costs nothing. Memory only: after a restart the next one is sent anyway.
        self._badges_sent: dict[str, int] = {}

    @property
    def web_enabled(self) -> bool:
        return bool(self._vapid_private_key and self._vapid_contact)

    @property
    def apns_enabled(self) -> bool:
        return self.apns is not None

    async def start(self) -> None:
        if self.apns is not None and self._worker is None:
            self._worker = asyncio.create_task(self._retry_loop())

    async def stop(self) -> None:
        # First, while the APNs client is still open: a count that was still settling goes out
        # now rather than leaving a phone's badge behind the restart.
        await self._badges.stop()
        if self._worker is not None:
            self._worker.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._worker
            self._worker = None
        if self._threads is not None:
            # Not waited on: a vendor that is not answering would hold shutdown for its whole
            # timeout, and the delivery is journalled or lost either way.
            self._threads.shutdown(wait=False, cancel_futures=True)
            self._threads = None
        if self.apns is not None:
            await self.apns.close()

    async def on_session_transition(self, transition: SessionTransition) -> None:
        kind = transition_kind(transition.previous_state, transition.state)
        if kind is None:
            return
        if transition.has_active_subscriber:
            log.debug("push suppressed: an app is watching this session", kind=kind)
            return
        if not transition.owner:
            log.debug("push skipped: the device has no owner", kind=kind)
            return
        device_id = str(transition.session.get("device_id") or "")
        await self.notify(
            kind, transition.session, await self._device_name(device_id), transition.owner
        )

    async def on_session_resume(self, notice: SessionResumeNotice) -> None:
        """A35: the device said what it did about a paused session, and the phone is told."""
        kind = resume_kind(notice.status)
        if kind is None:
            return
        if notice.has_active_subscriber:
            log.debug("push suppressed: an app is watching this session", kind=kind)
            return
        if not notice.owner:
            log.debug("push skipped: the device has no owner", kind=kind)
            return
        session = {"session_id": notice.session_id, "device_id": notice.device_id}
        await self.notify(kind, session, await self._device_name(notice.device_id), notice.owner)

    async def notify(
        self, kind: str, session: dict[str, Any], device_name: str, owner: str
    ) -> None:
        # Counted now, after the hub stored the change that cued this push (A47).
        badge = await self._badge_count(owner)
        payload = build_payload(kind, session, device_name, badge)
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        await asyncio.gather(
            self._deliver_web(body, owner),
            self._enqueue_apns(payload, owner),
            return_exceptions=True,
        )

    def on_badge_change(self, change: BadgeChange) -> None:
        """A47: an account's count moved; its phones hear it once it has settled."""
        if self.apns is not None and change.owner:
            self._badges.changed(change)

    async def push_badge(self, change: BadgeChange) -> None:
        """Tell the account's phones its count, unless the last push already did (A47, §3.7).

        APNs only: a browser shows every push it receives, so Web Push gets the count only inside
        the notifications it shows anyway.
        """
        badge = await self._badge_count(change.owner)
        if self._badges_sent.get(change.owner) == badge:
            return
        session = {"device_id": change.device_id, "session_id": change.session_id}
        payload = build_payload(
            KIND_BADGE, session, await self._device_name(change.device_id), badge
        )
        await self._enqueue_apns(payload, change.owner)

    # ---- web push ----

    async def _deliver_web(self, body: str, owner: str) -> None:
        if not self.web_enabled:
            return
        subscriptions = await self.store.list_web(owner)
        results = await asyncio.gather(
            *(self._deliver_one_web(item, body) for item in subscriptions),
            return_exceptions=True,
        )
        failures = sum(isinstance(item, Exception) for item in results)
        if failures:
            log.warning("web push delivery failed", failures=failures, total=len(results))

    async def _deliver_one_web(self, subscription: WebPushSubscription, body: str) -> None:
        status = await self._web_sender(subscription, body)
        if status in {404, 410}:
            await self.store.remove_web(subscription.endpoint, subscription.username)

    async def _send_with_pywebpush(
        self, subscription: WebPushSubscription, payload: str
    ) -> int | None:
        def send() -> int | None:
            from pywebpush import WebPushException, webpush

            try:
                response = webpush(
                    subscription_info=subscription.browser_payload(),
                    data=payload,
                    vapid_private_key=self._vapid_private_key,
                    vapid_claims={"sub": self._vapid_contact},
                    ttl=300,
                    timeout=WEB_PUSH_TIMEOUT_SECONDS,
                )
                raw = getattr(response, "status_code", None)
                return raw if isinstance(raw, int) else None
            except WebPushException as exc:
                failed = getattr(exc, "response", None)
                status = getattr(failed, "status_code", None)
                if isinstance(status, int) and status in {404, 410}:
                    return status
                raise

        return await asyncio.get_running_loop().run_in_executor(self._executor(), send)

    def _executor(self) -> ThreadPoolExecutor:
        """Push's own threads, created on first delivery so an idle gateway holds none."""
        if self._threads is None:
            self._threads = ThreadPoolExecutor(
                max_workers=PUSH_THREADS, thread_name_prefix="rc-push"
            )
        return self._threads

    # ---- apns ----

    async def _enqueue_apns(self, payload: dict[str, Any], owner: str) -> None:
        if self.apns is None:
            return
        registrations = await self.store.list_apns(owner)
        if not registrations:
            return
        rc = payload["rc"]
        body = json.dumps(_apns_payload(payload), ensure_ascii=False, separators=(",", ":"))
        for registration in registrations:
            await self.store.enqueue(
                registration.device_token,
                registration.environment,
                body,
                badge_only=rc["kind"] == KIND_BADGE,
            )
        self._badges_sent[owner] = rc["badge"]
        await self.flush_apns()

    async def flush_apns(self) -> None:
        """Send every due journal entry once, rescheduling or dropping by the APNs answer."""
        if self.apns is None:
            return
        for delivery in await self.store.claim_due():
            try:
                response = await self.apns.send(
                    delivery.device_token,
                    delivery.payload.encode("utf-8"),
                    environment=delivery.environment,
                    priority=PRIORITY_POWER_AWARE if delivery.badge_only else PRIORITY_IMMEDIATE,
                )
            except ValueError:
                await self.store.finish(delivery.delivery_id)
                await self.store.remove_apns(delivery.device_token)
                continue
            await self._settle(delivery.delivery_id, delivery, response)

    async def _settle(self, delivery_id: int, delivery: Any, response: ApnsResponse) -> None:
        if response.delivered:
            await self.store.finish(delivery_id)
            return
        if response.permanent_failure:
            log.info("apns registration dropped", reason=response.reason)
            await self.store.finish(delivery_id)
            await self.store.remove_apns(delivery.device_token)
            return
        attempts = delivery.attempts + 1
        if attempts >= MAX_ATTEMPTS:
            log.warning("apns delivery abandoned", attempts=attempts, status=response.status)
            await self.store.finish(delivery_id)
            return
        backoff = RETRY_BACKOFF_SECONDS[min(attempts - 1, len(RETRY_BACKOFF_SECONDS) - 1)]
        if response.retry_after is not None:
            backoff = max(backoff, response.retry_after)
        await self.store.retry_at(delivery_id, time.time() + backoff)

    async def _retry_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(WORKER_INTERVAL_SECONDS)
                await self.flush_apns()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("apns retry pass failed")


def _apns_payload(payload: dict[str, Any]) -> dict[str, Any]:
    rc = payload["rc"]
    # A47: the home-screen badge holds while the app is closed only because APNs sets it.
    if rc["kind"] == KIND_BADGE:
        # The count and nothing else: no alert, no sound (§3.7).
        return {"aps": {"badge": rc["badge"]}, **payload}
    return {
        "aps": {
            "alert": {"title": "SideCode", "body": rc["title"]},
            "sound": "default",
            "interruption-level": "active",
            "badge": rc["badge"],
        },
        **payload,
    }
