"""Which app connection each terminal streams to (amendment A38, §7.3).

A terminal is one person's view, never the account's: `terminal.output` reaches the connection
that opened or attached it and no other socket. The gateway learns the holder from the device's
accepted `terminal.open` and `terminal.attach` replies, forgets it when the shell exits, when the
terminal is closed and when the device goes away, and remembers which terminals it has already
asked a device to detach so a shell that keeps printing into a connection that is gone costs one
request rather than one per frame.

Nothing here reads a byte of what travels: the map is keyed by ids alone.
"""

from __future__ import annotations

#: ``(device_id, terminal_id)``: a terminal id is the device's to choose, so it is only unique
#: alongside the machine that minted it.
TerminalKey = tuple[str, str]

#: §7.3 allows four terminals per device and `devices.py` allows 64 devices per account, so this
#: clears what a fleet needs several times over. It is a cap rather than an assumption: both ids
#: come from a device, and a faulty one must not grow gateway memory without bound.
MAX_TRACKED_TERMINALS = 256


class TerminalRoutes:
    """The holder of every live terminal, and the detaches already asked for."""

    def __init__(self, capacity: int = MAX_TRACKED_TERMINALS) -> None:
        self._capacity = max(1, capacity)
        self._holders: dict[TerminalKey, str] = {}
        #: Used as an insertion-ordered set, so the oldest entry is the one eviction takes.
        self._detached: dict[TerminalKey, None] = {}

    def hold(self, device_id: str, terminal_id: str, connection_id: str) -> None:
        """Record who receives this terminal's output. A second `attach` simply moves it."""
        key = (device_id, terminal_id)
        self._detached.pop(key, None)
        self._holders.pop(key, None)
        while len(self._holders) >= self._capacity:
            del self._holders[next(iter(self._holders))]
        self._holders[key] = connection_id

    def holder(self, device_id: str, terminal_id: str) -> str | None:
        return self._holders.get((device_id, terminal_id))

    def release(self, device_id: str, terminal_id: str) -> None:
        """Forget a terminal that is gone: it exited, or it was closed."""
        key = (device_id, terminal_id)
        self._holders.pop(key, None)
        self._detached.pop(key, None)

    def release_connection(self, connection_id: str) -> list[TerminalKey]:
        """Forget every terminal one app socket held, and name them so they can be detached."""
        keys = [key for key, holder in self._holders.items() if holder == connection_id]
        for key in keys:
            del self._holders[key]
        return keys

    def release_device(self, device_id: str) -> None:
        """Forget a whole machine's terminals: its link is gone and so are its shells."""
        for key in [key for key in self._holders if key[0] == device_id]:
            del self._holders[key]
        for key in [key for key in self._detached if key[0] == device_id]:
            del self._detached[key]

    def note_detached(self, device_id: str, terminal_id: str) -> bool:
        """True the first time a terminal needs detaching, False while one is already asked for."""
        key = (device_id, terminal_id)
        if key in self._detached:
            return False
        while len(self._detached) >= self._capacity:
            del self._detached[next(iter(self._detached))]
        self._detached[key] = None
        return True
