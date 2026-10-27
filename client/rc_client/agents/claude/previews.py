"""How Claude Code's permission preview rewrites the text it shows.

The relay's `input_preview` is a tool's input made safe to read in a terminal,
and what it shows is not always what the tool was given: control, bidirectional
and invisible characters become a space, and the look-alikes of a quote, a
prime or an angle bracket become a middle dot. A call's words are found in a
preview only as the preview would have written them, so they are put through
the same rewriting before they are looked for. The sets are those of Claude
Code 2.1.310's own `truncateForPreview`, as code points; a character it leaves
alone is left alone here.
"""

from __future__ import annotations

import re

Span = int | tuple[int, int]

# Control, bidirectional and default-ignorable characters, and the blanks a font draws as nothing.
BLANKED: tuple[Span, ...] = (
    (0x00, 0x1F),
    (0x7F, 0x9F),
    0xAD,
    0x034F,
    0x061C,
    (0x115F, 0x1160),
    (0x17B4, 0x17B5),
    (0x180B, 0x180F),
    (0x200B, 0x200F),
    (0x2028, 0x202E),
    (0x2060, 0x206F),
    0x2800,
    0x3164,
    (0xFE00, 0xFE0F),
    0xFEFF,
    0xFFA0,
    (0xFFF0, 0xFFFC),
    0x13441,
    0x13442,
    0x16FE4,
    (0x1BCA0, 0x1BCA3),
    0x1D159,
    (0x1D173, 0x1D17A),
    (0xE0000, 0xE0FFF),
)

# The look-alikes of a double quote, of a single quote or a prime, and of an angle bracket.
LOOK_ALIKES: tuple[Span, ...] = (
    (0x201C, 0x201F),
    0x2E42,
    0xFF02,
    (0x02F5, 0x02F6),
    (0x2033, 0x2034),
    (0x2036, 0x2037),
    0x02BA,
    0x02DD,
    0x02EE,
    0x05F4,
    0x3003,
    (0x301D, 0x301F),
    (0x275D, 0x275E),
    0x2760,
    (0x1F676, 0x1F678),
    (0x2018, 0x201B),
    0x00B4,
    0x02B9,
    (0x02BB, 0x02C0),
    0x02C8,
    (0x02CA, 0x02CB),
    0x02F4,
    0x0374,
    0x0384,
    0x055A,
    0x055D,
    0x05F3,
    (0x07F4, 0x07F5),
    0x1FBD,
    0x1FBF,
    0x1FEF,
    (0x1FFD, 0x1FFE),
    0x2032,
    0x2035,
    (0x275B, 0x275C),
    0x275F,
    (0xA78B, 0xA78C),
    0xFF07,
    0xFF40,
    (0x02C2, 0x02C3),
    (0xFE64, 0xFE65),
    0xFF1C,
    0xFF1E,
)

MIDDLE_DOT = "·"


def _any_of(spans: tuple[Span, ...]) -> re.Pattern[str]:
    """A pattern for one character from these code points, or from these ranges of them."""
    members = (
        f"{re.escape(chr(span[0]))}-{re.escape(chr(span[1]))}"
        if isinstance(span, tuple)
        else re.escape(chr(span))
        for span in spans
    )
    return re.compile("[" + "".join(members) + "]")


_BLANKED = _any_of(BLANKED)
_LOOK_ALIKES = _any_of(LOOK_ALIKES)


def previewed(text: str) -> str:
    """The text as the preview writes it, before its runs of blanks are folded."""
    return _LOOK_ALIKES.sub(MIDDLE_DOT, _BLANKED.sub(" ", text))
