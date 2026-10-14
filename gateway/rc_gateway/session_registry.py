"""Revocation registry for signed session tokens.

The durable record lives in `AuthSessionStore`; this is the cache in front of it and the home of
the `asyncio.Event` that closes a live WebSocket the moment its session is signed out. A token
issued before a restart is therefore still valid: the first request that presents it misses the
cache, finds the row, and hydrates an entry.

Adapted from cc-remote's ``SessionRegistry`` (MIT), which was memory-only.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

from .auth import SessionClaims
from .auth_store import AuthSessionStore, StoredSession
from .logging import logger

log = logger("rc_gateway.sessions")

REGISTRY_CAP = 1024


@dataclass
class _Entry:
    expires_at: int
    seen_at: float = field(default_factory=time.monotonic)
    revoked: asyncio.Event = field(default_factory=asyncio.Event)


class SessionRegistry:
    def __init__(self, store: AuthSessionStore, cap: int = REGISTRY_CAP) -> None:
        self.store = store
        self.cap = cap
        self._entries: dict[str, _Entry] = {}
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        """Drop rows that can never authenticate again."""
        removed = await self.store.prune()
        if removed:
            log.info("pruned expired login sessions", removed=removed)

    async def register(self, claims: SessionClaims) -> bool:
        await self.store.add(
            StoredSession(jti=claims.jti, username=claims.username, expires_at=claims.expires_at)
        )
        async with self._lock:
            self._remember(claims.jti, claims.expires_at)
        return True

    async def active(self, claims: SessionClaims) -> bool:
        async with self._lock:
            self._prune(time.time())
            entry = self._entries.get(claims.jti)
            if entry is not None:
                entry.seen_at = time.monotonic()
                return entry.expires_at == claims.expires_at
        # A cache miss is the normal path for a token issued before the last restart.
        stored = await self.store.get(claims.jti)
        if stored is None or stored.expires_at != claims.expires_at:
            return False
        async with self._lock:
            self._remember(claims.jti, claims.expires_at)
        return True

    async def revoked_event(self, claims: SessionClaims) -> asyncio.Event | None:
        if not await self.active(claims):
            return None
        async with self._lock:
            entry = self._entries.get(claims.jti)
            return entry.revoked if entry is not None else None

    async def revoke(self, jti: str) -> bool:
        removed = await self.store.revoke(jti)
        async with self._lock:
            entry = self._entries.pop(jti, None)
        if entry is not None:
            entry.revoked.set()
        return removed or entry is not None

    async def revoke_for_user(self, username: str) -> int:
        """Sign one account out everywhere. Its live sockets close through the same events (A24)."""
        revoked = await self.store.revoke_for_user(username)
        async with self._lock:
            entries = [self._entries.pop(jti, None) for jti in revoked]
        for entry in entries:
            if entry is not None:
                entry.revoked.set()
        if revoked:
            log.info("revoked every login session of an account", sessions=len(revoked))
        return len(revoked)

    def _remember(self, jti: str, expires_at: int) -> None:
        """Cache one session, evicting the coldest entry rather than refusing a new login."""
        existing = self._entries.get(jti)
        if existing is not None:
            existing.seen_at = time.monotonic()
            return
        while len(self._entries) >= self.cap:
            coldest = min(self._entries.items(), key=lambda item: item[1].seen_at)[0]
            del self._entries[coldest]
        self._entries[jti] = _Entry(expires_at)

    def _prune(self, now: float) -> None:
        for jti, entry in list(self._entries.items()):
            if entry.expires_at <= now:
                del self._entries[jti]
