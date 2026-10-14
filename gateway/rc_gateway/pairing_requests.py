"""Claim tokens: the host asks to be paired, an app claims it by scanning (A23).

A host that cannot be typed at prints a QR code instead. It asks for a claim token, renders
``<public_origin>/pair#<token>`` and long-polls; a signed-in app claims that token, which mints the
ordinary pairing code for the host and hands it to the waiting poll. Enrolment then runs exactly as
it does for a typed code.

Tokens live in memory only. A gateway restart forgets them, the host's poll reads ``404`` and the
installer says to run the command again -- which is cheaper than a table that must be pruned, and
keeps an unauthenticated endpoint from writing to disk.
"""

from __future__ import annotations

import asyncio
import secrets
import time
from dataclasses import dataclass, field

from .devices import PAIR_ALPHABET

#: 128 bits of Crockford base32 (protocol/schema/objects.json, ``PairingToken``).
TOKEN_BYTES = 16
TOKEN_LENGTH = 26
TOKEN_TTL_SECONDS = 600
#: How long ``GET /api/pairing/requests/{token}`` holds a poll open before answering ``waiting``.
POLL_SECONDS = 25.0
#: The whole gateway, not per user: the endpoint that mints these is unauthenticated. Reaching it
#: no longer refuses the caller — the oldest unclaimed token is dropped instead, so an address
#: that hoards slots cannot take QR pairing away from everyone else for a whole TTL.
MAX_OUTSTANDING = 50
#: And per source address, so one address cannot hold more than a couple of the slots above. A
#: host prints one QR code at a time; three covers a retried installer.
MAX_OUTSTANDING_PER_ADDRESS = 3
#: Polls held open on one token at once. The host long-polls with a single request; more than a
#: couple means someone is pinning tasks on an unauthenticated endpoint.
MAX_WAITERS_PER_TOKEN = 2

_ALPHABET_SET = frozenset(PAIR_ALPHABET)


class TooManyWaiters(RuntimeError):
    """This claim token already holds as many long polls open as it is allowed."""


def generate_claim_token() -> str:
    value = int.from_bytes(secrets.token_bytes(TOKEN_BYTES), "big")
    characters = []
    for _ in range(TOKEN_LENGTH):
        value, index = divmod(value, len(PAIR_ALPHABET))
        characters.append(PAIR_ALPHABET[index])
    return "".join(reversed(characters))


def is_claim_token(value: str) -> bool:
    return len(value) == TOKEN_LENGTH and set(value) <= _ALPHABET_SET


@dataclass
class PairingRequest:
    """One host waiting to be claimed. ``code`` is set exactly once, by the claim."""

    token: str
    expires_at: int
    #: Who asked for it, so no address can hold more than its share of the outstanding slots.
    address: str = ""
    claimed: asyncio.Event = field(default_factory=asyncio.Event)
    claiming: bool = False
    code: str = ""
    code_expires_at: int = 0
    #: Polls currently held open on this token.
    waiters: int = 0

    def expired(self, now: int) -> bool:
        return self.expires_at <= now


class PairingRequests:
    """In-memory registry of outstanding claim tokens."""

    def __init__(self, *, ttl: int = TOKEN_TTL_SECONDS, poll_timeout: float = POLL_SECONDS) -> None:
        self.ttl = ttl
        self.poll_timeout = poll_timeout
        self._requests: dict[str, PairingRequest] = {}

    def __len__(self) -> int:
        return len(self._requests)

    def mint(self, address: str = "", *, now: int | None = None) -> PairingRequest | None:
        """Issue a token, or None when this address already holds its share of them.

        The global cap is not a refusal: an unauthenticated caller that filled it would otherwise
        take QR pairing away from every real host for a whole TTL, so the oldest unclaimed token
        is dropped to make room. The per-address cap is what actually refuses, and it refuses only
        the address doing the hoarding.
        """
        moment = _now(now)
        self._expire(moment)
        if self._held_by(address) >= MAX_OUTSTANDING_PER_ADDRESS:
            return None
        if len(self._requests) >= MAX_OUTSTANDING and not self._drop_oldest_unclaimed():
            return None
        request = PairingRequest(
            token=generate_claim_token(), expires_at=moment + self.ttl, address=address
        )
        self._requests[request.token] = request
        return request

    def _held_by(self, address: str) -> int:
        """Outstanding tokens from one address. An empty address is nobody in particular."""
        if not address:
            return 0
        return sum(1 for item in self._requests.values() if item.address == address)

    def _drop_oldest_unclaimed(self) -> bool:
        """Free one slot, or report that every outstanding token is already spoken for."""
        for token, item in self._requests.items():
            if not item.code and not item.claiming:
                del self._requests[token]
                return True
        return False

    def find(self, token: str) -> PairingRequest | None:
        """The request for a token, expired or not, so a poll can tell ``410`` from ``404``."""
        return self._requests.get(token)

    def begin_claim(self, token: str, *, now: int | None = None) -> str:
        """Reserve a token for the caller. Returns an error code or an empty string on success.

        The reservation is taken without awaiting anything, so two claims arriving together
        cannot both mint a pairing code for the same host.
        """
        request = self.find(token)
        if request is None or request.expired(_now(now)):
            return "not_found"
        if request.code or request.claiming:
            return "conflict"
        request.claiming = True
        return ""

    def fulfil(self, token: str, code: str, expires_at: int) -> None:
        """Attach the minted code and wake the host's poll."""
        request = self._requests.get(token)
        if request is None:
            return
        request.code = code
        request.code_expires_at = expires_at
        request.claiming = False
        request.claimed.set()

    def abandon(self, token: str) -> None:
        """Release a reservation whose claim failed, so the host can still be claimed."""
        request = self._requests.get(token)
        if request is not None:
            request.claiming = False

    def spend(self, token: str) -> None:
        """Forget a token whose code has been delivered to the host."""
        self._requests.pop(token, None)

    async def wait_for_claim(self, request: PairingRequest) -> bool:
        """Hold the host's poll open until the token is claimed or the poll times out.

        Raises ``TooManyWaiters`` when this token already has as many polls parked on it as it is
        allowed. The route is unauthenticated and each poll pins a task for 25 s, so without the
        cap one token turns a modest request rate into a large standing count of them.
        """
        if request.code:
            return True
        if request.waiters >= MAX_WAITERS_PER_TOKEN:
            raise TooManyWaiters(request.token)
        request.waiters += 1
        try:
            await asyncio.wait_for(request.claimed.wait(), timeout=self.poll_timeout)
        except TimeoutError:
            return False
        finally:
            request.waiters -= 1
        return bool(request.code)

    def _expire(self, now: int) -> None:
        for token in [key for key, item in self._requests.items() if item.expired(now)]:
            del self._requests[token]


def _now(value: int | None) -> int:
    return int(time.time()) if value is None else value
