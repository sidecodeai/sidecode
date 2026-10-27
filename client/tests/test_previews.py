"""What Claude Code's permission preview does to the text it shows, and that a call's words match.

The preview is where a prompt's call is told from another's, so a text the CLI rewrites must be
rewritten the same way before it is looked for; otherwise a call with a typographic quote in its
input is never found in the prompt that is about it.
"""

from __future__ import annotations

import pytest

from rc_client.agents.claude.calls import calls_in, fits, needles_of
from rc_client.agents.claude.previews import BLANKED, LOOK_ALIKES, MIDDLE_DOT, Span, previewed

from .test_claude_calls import made

DOT = 0xB7


def text(*parts: str | int) -> str:
    """A string from literal parts and code points, so the odd characters are named, not drawn."""
    return "".join(chr(part) if isinstance(part, int) else part for part in parts)


def members(spans: tuple[Span, ...]) -> list[int]:
    return [
        code
        for span in spans
        for code in (range(span[0], span[1] + 1) if isinstance(span, tuple) else [span])
    ]


def test_every_control_bidirectional_and_invisible_character_becomes_a_space() -> None:
    codes = members(BLANKED)
    assert len(codes) > 4000, "the tag characters alone are a block of them"
    for code in codes:
        assert previewed(chr(code)) == " ", hex(code)


def test_every_look_alike_of_a_quote_a_prime_or_an_angle_bracket_becomes_a_middle_dot() -> None:
    for code in members(LOOK_ALIKES):
        assert previewed(chr(code)) == MIDDLE_DOT, hex(code)


@pytest.mark.parametrize(
    ("said", "shown"),
    [
        (text("don", 0x2019, "t panic"), text("don", DOT, "t panic")),
        (
            text(0x201C, "quoted", 0x201D, " and ", 0x201E, "quoted", 0x201F),
            text(DOT, "quoted", DOT, " and ", DOT, "quoted", DOT),
        ),
        (text("5", 0x2032, " 3", 0x2033, " ", 0x2035), text("5", DOT, " 3", DOT, " ", DOT)),
        (
            text("a ", 0x2C2, "b", 0x2C3, " ", 0xFE64, "c", 0xFE65, " ", 0xFF1C, "d", 0xFF1E),
            text("a ", DOT, "b", DOT, " ", DOT, "c", DOT, " ", DOT, "d", DOT),
        ),
        ("tab\there\x1b[0m", "tab here [0m"),
        (text("zero", 0x200B, "width", 0x202E, "flipped", 0x2066), "zero width flipped "),
        (text("soft", 0xAD, "hyphen", 0xFEFF, "bom"), "soft hyphen bom"),
    ],
    ids=["apostrophe", "double quotes", "primes", "angle brackets", "control", "bidi", "invisible"],
)
def test_the_text_is_rewritten_as_the_preview_writes_it(said: str, shown: str) -> None:
    assert previewed(said) == shown


def test_what_the_preview_leaves_alone_is_left_alone() -> None:
    plain = text("say \"hi\" 'there' <b>caf", 0xE9, "</b> ", 0x2014, " ", 0x4E2D, 0x6587, " ")
    plain += text(0x1F600, " `tick` $HOME \\n")
    assert previewed(plain) == plain


def test_a_call_with_a_typographic_quote_fits_the_preview_the_cli_writes_for_it() -> None:
    edit = {
        "file_path": "/repo/NOTES.md",
        "old_string": text("don", 0x2019, "t panic"),
        "new_string": "stay calm",
    }
    [call] = calls_in([made("t1", "Edit", edit)]).values()
    shown = text(
        '{ "file_path": "/repo/NOTES.md", "old_string": "don',
        DOT,
        't panic", "new_string": "stay calm" }',
    )
    assert fits(shown, call)
    assert not fits(shown.replace(text("don", DOT), "do not"), call)


def test_a_needle_is_what_the_preview_shows_of_the_text_and_nothing_longer() -> None:
    said = text("echo ", 0x201C, "hello", 0x201D, 0x200B, "  now")
    assert needles_of({"command": said}) == (text('"echo ', DOT, "hello", DOT, ' now"'),)
