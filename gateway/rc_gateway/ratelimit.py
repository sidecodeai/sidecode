"""Bounded per-IP attempt limiter.

Adapted from cc-remote's ``LoginRateLimiter`` (MIT). Three caps matter: per IP (the actual limit),
per number of tracked IPs and a global attempt ceiling, so a distributed guessing run cannot turn
the limiter itself into unbounded memory.
"""

from __future__ import annotations

import time
from collections import defaultdict

WINDOW_SECONDS = 60.0
MAX_PER_IP = 5
MAX_IPS = 4096
MAX_TOTAL_ATTEMPTS = 16384
CLEANUP_INTERVAL = 30.0


class RateLimiter:
    def __init__(
        self,
        *,
        window: float = WINDOW_SECONDS,
        max_per_ip: int = MAX_PER_IP,
        max_ips: int = MAX_IPS,
        max_total_attempts: int = MAX_TOTAL_ATTEMPTS,
        cleanup_interval: float = CLEANUP_INTERVAL,
    ) -> None:
        self.window = window
        self.max_per_ip = max_per_ip
        self.max_ips = max_ips
        self.max_total_attempts = max_total_attempts
        self.cleanup_interval = cleanup_interval
        self._attempts: dict[str, list[float]] = defaultdict(list)
        self._total = 0
        self._last_cleanup = 0.0

    def reset(self) -> None:
        self._attempts.clear()
        self._total = 0
        self._last_cleanup = 0.0

    def _cleanup(self, now: float) -> None:
        cutoff = now - self.window
        total = 0
        for ip in list(self._attempts):
            fresh = [item for item in self._attempts[ip] if item > cutoff]
            if fresh:
                self._attempts[ip] = fresh
                total += len(fresh)
            else:
                del self._attempts[ip]
        self._total = total
        self._last_cleanup = now

    def limited(self, ip: str, *, now: float | None = None) -> bool:
        """Record an attempt and report whether it must be rejected."""
        current = time.time() if now is None else now
        if current - self._last_cleanup >= self.cleanup_interval:
            self._cleanup(current)

        attempts = self._attempts.get(ip)
        if attempts is not None:
            cutoff = current - self.window
            fresh = [item for item in attempts if item > cutoff]
            self._total -= len(attempts) - len(fresh)
            if fresh:
                self._attempts[ip] = fresh
                attempts = fresh
            else:
                del self._attempts[ip]
                attempts = None

        if attempts is not None and len(attempts) >= self.max_per_ip:
            return True
        if attempts is None and len(self._attempts) >= self.max_ips:
            return True
        if self._total >= self.max_total_attempts:
            return True

        if attempts is None:
            attempts = []
            self._attempts[ip] = attempts
        attempts.append(current)
        self._total += 1
        return False
