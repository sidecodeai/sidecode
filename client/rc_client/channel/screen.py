"""What the device can read of a terminal it owns: its text, and its keyboard.

The pseudo-terminal proxy (amendment A40) has to answer two questions about a
live Claude Code session: what is on the screen, so a picker can be driven, and
whether the person at the keyboard is in the middle of something, so the device
stays off it. Neither answer may leak what was typed: `Keyboard` counts
characters and never keeps them.

`Screen` is not a terminal emulator. It removes the escape sequences and keeps
the text, turning horizontal moves into spaces and vertical ones into line
breaks. Claude Code's TUI lays a line out by jumping the cursor between words
(`CSI n G`) rather than by writing spaces, so nothing else reconstructs a row
like `1. Sonnet` from what comes down the wire. A redraw appends, so the last
time a row was written is what is on screen now.

`Keyboard` counts what a person typed, and only that. A terminal answers the
CLI's own questions on the same file descriptor — `CSI c` asks what it is and
the answer arrives looking exactly like keystrokes — so escape sequences are
read and dropped rather than counted; otherwise a terminal that reported its
version once would look busy for as long as it lived.

Both classes are pure and stdlib-only: the proxy runs before the CLI does, and
anything imported here is time the terminal spends not starting.
"""

from __future__ import annotations

import time

# Keys that end whatever the person was typing. Escape ends it too, but it
# also starts every sequence a terminal sends, so it is read, not listed here.
ENTER = 0x0D
NEWLINE = 0x0A
ESCAPE = 0x1B
CTRL_C = 0x03
CTRL_U = 0x15
RESET_KEYS = frozenset({ENTER, NEWLINE, CTRL_C, CTRL_U})
ERASE_KEYS = frozenset({0x7F, 0x08})

# CSI final bytes that move the cursor along a line, or clear part of one: the
# TUI uses them where a layout would use spaces.
_SPACING_FINALS = frozenset(b"CDGI`a")
# CSI final bytes that move the cursor off the line it is on.
_BREAKING_FINALS = frozenset(b"ABEFHJfd")

_NORMAL, _ESC, _CSI, _SS3, _STRING, _STRING_ESC = range(6)
# `ESC ]` (OSC) and its siblings run until a string terminator rather than a
# final byte, so they are consumed by the same state.
_STRING_STARTS = frozenset(b"]P^_X")


def _is_final(byte: int) -> bool:
    return 0x40 <= byte <= 0x7E


class Screen:
    """The child's output with escapes removed, capped at the most recent bytes."""

    __slots__ = ("_limit", "_state", "_text")

    def __init__(self, limit: int = 8 * 1024) -> None:
        self._limit = limit
        self._text = bytearray()
        self._state = _NORMAL

    def feed(self, chunk: bytes) -> None:
        for byte in chunk:
            self._byte(byte)
        if len(self._text) > self._limit * 2:
            del self._text[: len(self._text) - self._limit]

    def text(self) -> str:
        """The tail, decoded leniently: a cut multi-byte character is not an error."""
        return bytes(self._text[-self._limit :]).decode("utf-8", "replace")

    def reset(self) -> None:
        """Forget what is there, so what comes next can be read on its own."""
        self._text.clear()

    def _byte(self, byte: int) -> None:
        if self._state == _NORMAL:
            self._plain(byte)
        elif self._state == _ESC:
            self._after_escape(byte)
        elif self._state == _CSI:
            if _is_final(byte):
                self._state = _NORMAL
                if byte in _BREAKING_FINALS:
                    self._text += b"\n"
                elif byte in _SPACING_FINALS:
                    self._text += b" "
        elif self._state == _SS3:
            self._state = _NORMAL
        elif self._state == _STRING:
            if byte == 0x07:
                self._state = _NORMAL
            elif byte == ESCAPE:
                self._state = _STRING_ESC
        else:  # _STRING_ESC: only `ESC \` ends the string
            self._state = _NORMAL if byte == 0x5C else _STRING

    def _plain(self, byte: int) -> None:
        if byte == ESCAPE:
            self._state = _ESC
        elif byte == ENTER:
            # A carriage return rewrites the line the TUI just drew.
            self._text += b"\n"
        elif byte in (NEWLINE, 0x09) or (byte >= 0x20 and byte != 0x7F):
            self._text.append(byte)

    def _after_escape(self, byte: int) -> None:
        if byte == 0x5B:  # `[`
            self._state = _CSI
        elif byte == 0x4F:  # `O`
            self._state = _SS3
        elif byte in _STRING_STARTS:
            self._state = _STRING
        else:
            # A two-byte sequence such as `ESC =`: both bytes are gone.
            self._state = _NORMAL


class Keyboard:
    """How far into something the person at the keyboard is.

    `draft` is the characters they have typed since they last finished or
    abandoned a line; `idle_for` is how long the keyboard has been quiet. The
    device types only into a terminal whose draft is empty and whose keyboard
    has been still, which is the whole point of counting.
    """

    __slots__ = ("_state", "draft", "last_key_at")

    def __init__(self) -> None:
        self.draft = 0
        self.last_key_at = time.monotonic()
        self._state = _NORMAL

    def feed(self, chunk: bytes) -> None:
        self.last_key_at = time.monotonic()
        for byte in chunk:
            self._byte(byte)
        if self._state == _ESC:
            # A terminal writes a sequence in one go, so an escape alone at the
            # end of a read is the Escape key: the line is abandoned.
            self._state = _NORMAL
            self.draft = 0

    def _byte(self, byte: int) -> None:
        if self._state == _ESC:
            self._after_escape(byte)
        elif self._state == _CSI:
            if _is_final(byte):
                self._state = _NORMAL
        elif self._state == _SS3:
            self._state = _NORMAL
        elif self._state == _STRING:
            if byte == 0x07:
                self._state = _NORMAL
            elif byte == ESCAPE:
                self._state = _STRING_ESC
        elif self._state == _STRING_ESC:
            self._state = _NORMAL if byte == 0x5C else _STRING
        elif byte == ESCAPE:
            self._state = _ESC
        elif byte in RESET_KEYS:
            self.draft = 0
        elif byte in ERASE_KEYS:
            self.draft = max(0, self.draft - 1)
        elif byte >= 0x20 and byte != 0x7F:
            self.draft += 1

    def _after_escape(self, byte: int) -> None:
        if byte == 0x5B:
            self._state = _CSI
        elif byte == 0x4F:
            self._state = _SS3
        elif byte in _STRING_STARTS:
            self._state = _STRING
        else:
            # Escape and then a key: the person left whatever they were typing.
            self._state = _NORMAL
            self.draft = 0

    @property
    def idle_for(self) -> float:
        return max(0.0, time.monotonic() - self.last_key_at)
