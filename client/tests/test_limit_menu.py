"""Amendment A63: Claude Code's usage-limit menu, read off the terminal's screen."""

from __future__ import annotations

import pytest

from rc_client.channel.screen import Screen
from rc_client.sessions import limit_menu
from rc_client.sessions.limit_menu import (
    UNKNOWN,
    Row,
    card_label,
    find_menu,
    foreign,
    kind_of,
    read_menu,
    shows_dialog,
)

from .limit_helpers import (
    ADMIN,
    CONVERSATION,
    CREDITS,
    FOOTER,
    FUNDS,
    HIGHLIGHT,
    NO_AUTO,
    PROMPT_BOX,
    STOP,
    STOP_WAIT,
    TITLE,
    UPGRADE,
    WAIT_AT,
    WAIT_RESET,
    WAIT_SHORTLY,
    menu_text,
)


def kinds(text: str) -> list[tuple[int, str]]:
    menu = read_menu(text)
    assert menu is not None, "the menu was not read"
    return [(row.number, row.kind) for row in menu.rows]


# ---------------------------------------------------------------- the rows


@pytest.mark.parametrize(
    ("rows", "expected"),
    [
        (
            (STOP_WAIT, WAIT_AT, CREDITS),
            [(1, "stop_wait"), (2, "wait"), (3, "credits")],
        ),
        (
            (WAIT_SHORTLY, STOP_WAIT, CREDITS, UPGRADE),
            [(1, "wait"), (2, "stop_wait"), (3, "credits"), (4, "upgrade")],
        ),
        (
            (STOP, WAIT_RESET, UPGRADE),
            [(1, "stop"), (2, "wait"), (3, "upgrade")],
        ),
        (
            (NO_AUTO, STOP, FUNDS),
            [(1, "no_auto"), (2, "stop"), (3, "funds")],
        ),
        (
            (WAIT_AT, STOP_WAIT, ADMIN),
            [(1, "wait"), (2, "stop_wait"), (3, "admin")],
        ),
    ],
    ids=["stop-wait-first", "wait-first", "plain-stop", "no-auto", "admin"],
)
def test_each_menu_claude_code_draws_is_read_row_by_row(
    rows: tuple[str, ...], expected: list[tuple[int, str]]
) -> None:
    """Which options there are, and their order, change with the plan and the account."""
    assert kinds(CONVERSATION + menu_text(rows)) == expected


def test_stop_is_not_stop_and_wait() -> None:
    assert kind_of("Stop") == "stop"
    assert kind_of(STOP_WAIT) == "stop_wait"
    assert kind_of("Stop the build") == UNKNOWN
    assert kind_of("Don't stop") == UNKNOWN


@pytest.mark.parametrize(
    ("label", "kind"),
    [
        (WAIT_AT, "wait"),
        (WAIT_SHORTLY, "wait"),
        (WAIT_RESET, "wait"),
        (NO_AUTO, "no_auto"),
        ("Don\u2019t continue automatically", "no_auto"),
        (CREDITS, "credits"),
        (UPGRADE, "upgrade"),
        (FUNDS, "funds"),
        (ADMIN, "admin"),
        ("Continue automatically when your plan renews", UNKNOWN),
        ("Log in with a different account", UNKNOWN),
        ("", UNKNOWN),
    ],
)
def test_a_row_is_known_by_the_stable_start_of_its_wording(label: str, kind: str) -> None:
    assert kind_of(label) == kind


def test_the_numbers_are_the_screen_s_whatever_the_order() -> None:
    menu = read_menu(menu_text((CREDITS, WAIT_AT, STOP_WAIT)))
    assert menu is not None
    wait = menu.row("wait")
    stop_wait = menu.row("stop_wait")
    assert wait is not None and stop_wait is not None
    assert (wait.digit, stop_wait.digit) == ("2", "3")


@pytest.mark.parametrize(
    ("label", "kind"),
    [
        ("Stop and wait for limit to r\u2026", "stop_wait"),
        ("Wait here, then cont\u2026", "wait"),
        ("Wait here, then continue automatically at 6:3\u2026", "wait"),
        ("Switch to usage cr...", "credits"),
        ("Stop\u2026", UNKNOWN),
        ("Wa\u2026", UNKNOWN),
    ],
)
def test_a_label_the_width_cut_is_known_by_what_is_left(label: str, kind: str) -> None:
    assert kind_of(label) == kind


def test_a_label_the_width_wrapped_is_joined_to_its_row() -> None:
    text = menu_text(
        (STOP_WAIT, "Wait here, then", CREDITS),
    ).replace(
        " 2. Wait here, then\n",
        " 2. Wait here, then\n\n      continue automatically when the limit resets\n",
    )
    menu = read_menu(text)
    assert menu is not None
    assert [(row.number, row.kind) for row in menu.rows] == [
        (1, "stop_wait"),
        (2, "wait"),
        (3, "credits"),
    ]
    wait = menu.row("wait")
    assert wait is not None
    assert wait.label == "Wait here, then continue automatically when the limit resets"


def test_a_wrapped_stable_start_is_joined_until_it_is_known() -> None:
    text = menu_text((STOP_WAIT, "Wait here,", CREDITS)).replace(
        " 2. Wait here,\n", " 2. Wait here,\n\n      then continue\n\n      automatically\n"
    )
    menu = read_menu(text)
    assert menu is not None
    assert [row.kind for row in menu.rows] == ["stop_wait", "wait", "credits"]


def test_words_a_redraw_broke_are_read_around_the_gaps() -> None:
    """Claude Code skips cells that did not change, and the proxy writes a space for the jump."""
    text = (
        " What do you want to d ?\n\n\n\n"
        f" {HIGHLIGHT} 1. Stop and wait f r limit to reset\n\n"
        "   2. Wait here, then continue automat cally at 6:30pm\n\n"
        "   3. Switch to usage credits\n\n\n\n"
        " Enter to confi m \u00b7 Esc to cancel\n"
    )
    menu = read_menu(text)
    assert menu is not None
    assert [row.kind for row in menu.rows] == ["stop_wait", "wait", "credits"]
    # A label the redraw broke is shown in the option's own words.
    stop_wait, wait = menu.free
    assert card_label(stop_wait) == STOP_WAIT
    assert card_label(wait) == "Wait here, then continue automatically"


def test_a_free_trial_s_end_is_not_the_usage_limit_menu() -> None:
    """It asks under the same title, with the same footer, and none of the limit's own rows."""
    trial = menu_text(("Upgrade your plan", "Log in with a different account", "Exit"))
    assert read_menu(trial) is None
    assert find_menu(trial) is None
    with_stop = menu_text((UPGRADE, STOP))
    assert read_menu(with_stop) is None


def test_a_menu_drawn_over_is_not_the_menu_on_screen() -> None:
    text = CONVERSATION + menu_text((STOP_WAIT, WAIT_AT, CREDITS)) + PROMPT_BOX
    assert read_menu(text) is None
    found = find_menu(text)
    assert found is not None and not found.last


def test_the_highlight_moving_is_still_the_menu() -> None:
    """Moving it redraws the rows it left and reached, which are the menu's own words."""
    text = (
        menu_text((STOP_WAIT, WAIT_AT, CREDITS))
        + f"\n\n   1. {STOP_WAIT}\n\n {HIGHLIGHT} 2. {WAIT_AT}\n"
        + f"\n\n {HIGHLIGHT} 2. Wait here, then contin e automatically\n"
    )
    menu = read_menu(text)
    assert menu is not None and menu.last


def test_the_last_menu_drawn_is_the_one_read() -> None:
    first = menu_text((STOP_WAIT, CREDITS))
    second = menu_text((WAIT_AT, STOP_WAIT, CREDITS))
    menu = read_menu(first + PROMPT_BOX + second)
    assert menu is not None
    assert [row.kind for row in menu.rows] == ["wait", "stop_wait", "credits"]


def test_a_menu_still_being_drawn_is_not_read_yet() -> None:
    text = menu_text((STOP_WAIT, WAIT_AT, CREDITS))
    assert read_menu(text.split(" Enter")[0]) is None
    assert read_menu(text.replace(f" {TITLE}", " ")) is None


def test_an_app_is_offered_the_free_options_in_screen_order_once_each() -> None:
    menu = read_menu(menu_text((CREDITS, WAIT_AT, UPGRADE, STOP_WAIT, STOP, ADMIN)))
    assert menu is not None
    assert [(row.kind, card_label(row)) for row in menu.free] == [
        ("wait", WAIT_AT),
        ("stop_wait", STOP_WAIT),
        ("stop", STOP),
    ]


def test_the_menu_as_the_proxy_reads_claude_code_s_own_bytes() -> None:
    """Rows laid out by jumping the cursor, lines by a return and a move down."""
    grey, white, reset = "\x1b[38;2;153;153;153m", "\x1b[38;2;255;255;255m", "\x1b[39m"
    raw = (
        "\x1b[?25l\x1b[H\r\x1b[30B"
        f"{white}\x1b[1mWhat\x1b[7Gdo\x1b[10Gyou\x1b[14Gwant\x1b[19Gto\x1b[22Gdo?\x1b[22m\r\x1b[2B"
        f"\x1b[3G{white}{HIGHLIGHT}\x1b[5G1.\x1b[8GStop\x1b[13Gand\x1b[17Gwait\x1b[22Gfor"
        "\x1b[26Glimit\x1b[32Gto\x1b[35Greset\r\x1b[1B"
        f"\x1b[5G{grey}2.\x1b[8GWait\x1b[13Ghere,\x1b[19Gthen\x1b[24Gcontinue"
        "\x1b[33Gautomatically\x1b[47Gat\x1b[50G6:30pm\r\x1b[1B"
        "\x1b[5G3.\x1b[8GSwitch\x1b[15Gto\x1b[18Gusage\x1b[24Gcredits\r\x1b[2B"
        f"\x1b[3G{grey}Enter to confirm \u00b7 Esc to cancel{reset}\x1b[40;1H"
    )
    screen = Screen()
    screen.feed(raw.encode("utf-8"))
    menu = read_menu(screen.text())
    assert menu is not None
    assert [(row.number, row.kind) for row in menu.rows] == [
        (1, "stop_wait"),
        (2, "wait"),
        (3, "credits"),
    ]
    assert [card_label(row) for row in menu.free] == [STOP_WAIT, WAIT_AT]


# ------------------------------------------------------- what else is said


def test_a_menu_with_rows_nobody_knows_is_still_seen_as_a_menu() -> None:
    """Its title and footer are up and nothing was drawn over them: somebody has to answer."""
    unknown = menu_text(("Something new", "Something else"))
    assert read_menu(unknown) is None
    assert shows_dialog(unknown)
    assert not shows_dialog(unknown + PROMPT_BOX)
    assert not shows_dialog(CONVERSATION)


def test_only_a_word_the_menu_does_not_have_is_something_else() -> None:
    known = limit_menu.vocabulary([WAIT_AT])
    assert not foreign([" 2. Wait here, then contin e automatic", "\u2500\u2500", "1. 2."], known)
    assert not foreign(["at 6:30pm", "to"], known)
    assert foreign(["  ? for shortcuts"], known)


def test_a_row_past_the_ninth_has_no_key() -> None:
    assert Row(number=9, label=STOP, kind="stop").digit == "9"
    assert Row(number=10, label=STOP, kind="stop").digit is None


def test_the_footer_and_title_are_their_own_lines() -> None:
    assert limit_menu.is_title(f" {TITLE}")
    assert limit_menu.is_footer(f" {FOOTER}")
    assert not limit_menu.is_footer("Enter to set as default \u00b7 Esc to cancel and go back home")
    assert not limit_menu.is_title("What do you want to do next with the parser and the tests?")


def test_only_a_row_whose_words_are_all_there_is_whole() -> None:
    """A key only ever goes to words the device has read whole."""
    menu = read_menu(menu_text((STOP_WAIT, "Wait here, then contin e automatically at 6:30pm")))
    assert menu is not None
    stop_wait, wait = menu.free
    assert limit_menu.whole(stop_wait)
    assert not limit_menu.whole(wait)
    assert not limit_menu.whole(Row(number=3, label="Something new", kind=UNKNOWN))
