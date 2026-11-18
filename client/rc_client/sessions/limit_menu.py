"""Claude Code's usage-limit menu, as the terminal shows it (amendment A63).

When Claude Code stops at the vendor's usage limit and has no automatic
continue left, it asks in its own terminal what to do, and the session waits
there until somebody answers. The device answers it from an app by pressing the
option's number in the pseudo-terminal it owns (A40), so it has to know the
menu when it sees one, and never take anything else for it.

Everything here is pure and reads the text the proxy keeps of the screen
(`channel/screen.py`). That text is not a terminal emulator's: Claude Code
redraws only the cells that changed, the proxy turns each jump over unchanged
cells into one space, so a word the screen already half showed comes out
broken ("confi m"), and every redraw is appended to what came before. So the
menu is read from its last whole frame, word by word with gaps allowed, and it
is the menu only when three things are there together:

- its title, "What do you want to do?", which the end of a free trial asks too;
- at least one numbered row that only the usage-limit menu has;
- its footer, "Enter to confirm · Esc to cancel".

Rows are known by stable prefixes, never by their whole wording: which options
the menu offers, their order and the time a row names change with the plan, the
account's usage and Claude Code's own experiments. A row that costs money or
opens a browser is known so that it is never offered or chosen, and a row
nobody knows is neither.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

TITLE = "What do you want to do?"
FOOTER = "Enter to confirm \u00b7 Esc to cancel"
ELLIPSES = ("\u2026", "...")
UNKNOWN = "unknown"
# How much of an option's wording a row has to show, in order, to be that option.
MATCH = 0.6
# How many of the title's and the footer's six words may be broken or missing.
LINE_SLACK = 1
# A word this short says nothing about whether the menu is still what is drawn.
MIN_FOREIGN = 3
# What frames a row: borders, the highlight marker and the like, never words.
_FRAME = " \t\u2502\u2503\u2551\u258f\u2595\u276f\u203a>\u25b8\u2022*\u00b7"
_WORD = re.compile(r"[a-z0-9]+")
# A numbered row: anything that is not a word, the number, a dot, then the label.
_ROW = re.compile(r"^\W*?(\d{1,2})\.\s+(\S.*)$")


@dataclass(frozen=True, slots=True)
class Option:
    """One option the menu is known to offer, by the stable start of its wording.

    `free` options cost nothing and open nothing, and are the only ones an app
    is offered. `only_here` is an option no other menu has, which is what tells
    this menu from the one a free trial's end shows under the same title.
    `whole` is an option whose row says that word and nothing more.
    """

    kind: str
    text: str
    free: bool
    only_here: bool
    whole: bool = False


OPTIONS = (
    Option("wait", "Wait here, then continue automatically", free=True, only_here=True),
    Option("stop_wait", "Stop and wait for limit to reset", free=True, only_here=True),
    Option("no_auto", "Don't continue automatically", free=True, only_here=True),
    Option("stop", "Stop", free=True, only_here=False, whole=True),
    Option("credits", "Switch to usage credits", free=False, only_here=True),
    Option("upgrade", "Upgrade your plan", free=False, only_here=False),
    Option("funds", "Add funds", free=False, only_here=False),
    Option("admin", "Ask your admin", free=False, only_here=False),
)
_BY_KIND = {option.kind: option for option in OPTIONS}


@dataclass(frozen=True, slots=True)
class Row:
    """A numbered row as the screen shows it: its number, its words and what it is."""

    number: int
    label: str
    kind: str

    @property
    def free(self) -> bool:
        option = _BY_KIND.get(self.kind)
        return option is not None and option.free

    @property
    def digit(self) -> str | None:
        """The key that picks this row, which Claude Code takes without Enter."""
        return str(self.number) if 1 <= self.number <= 9 else None


@dataclass(frozen=True, slots=True)
class Menu:
    """The usage-limit menu, row by row in the order the screen shows them."""

    rows: tuple[Row, ...]
    # The lines the screen drew after the menu's footer.
    after: tuple[str, ...] = ()

    def row(self, kind: str) -> Row | None:
        return next((row for row in self.rows if row.kind == kind), None)

    @property
    def free(self) -> tuple[Row, ...]:
        """The options an app may offer: free ones, each kind once, in screen order."""
        seen: set[str] = set()
        rows: list[Row] = []
        for row in self.rows:
            if row.free and row.kind not in seen:
                seen.add(row.kind)
                rows.append(row)
        return tuple(rows)

    @property
    def vocabulary(self) -> frozenset[str]:
        """Every word the menu itself draws, which a redraw of it may repeat."""
        return vocabulary(row.label for row in self.rows)

    @property
    def last(self) -> bool:
        """Whether nothing foreign to the menu was drawn after it."""
        return not foreign(self.after, self.vocabulary)


def words(text: str) -> list[str]:
    """The words of a line, lower case, with every apostrophe made one."""
    return _WORD.findall(text.lower().replace("\u2019", "'"))


def vocabulary(labels: Iterable[str] = ()) -> frozenset[str]:
    """The words of the title, the footer, every known option and these labels."""
    texts = [TITLE, FOOTER, *(option.text for option in OPTIONS), *labels]
    return frozenset(word for text in texts for word in words(text))


def in_order(seen: Sequence[str], expected: Sequence[str]) -> int:
    """How many of `expected` appear whole in `seen`, in the same order."""
    found = 0
    position = 0
    for word in expected:
        try:
            position = seen.index(word, position) + 1
        except ValueError:
            continue
        found += 1
    return found


def _is_line(text: str, expected: str) -> bool:
    """Whether a screen line is this one line of the menu, a word broken or missing at most."""
    seen = words(text)
    wanted = words(expected)
    found = in_order(seen, wanted)
    return found >= len(wanted) - LINE_SLACK and len(seen) <= len(wanted) + 2 * LINE_SLACK + 2


def is_title(text: str) -> bool:
    return _is_line(text, TITLE)


def is_footer(text: str) -> bool:
    return _is_line(text, FOOTER)


def clean_label(text: str) -> str:
    """A row's label with its white space made single and its frame taken off."""
    return " ".join(text.split()).strip(_FRAME)


def kind_of(label: str) -> str:
    """Which known option a row's label is, or `UNKNOWN`."""
    return _match(label)[0]


def _match(label: str) -> tuple[str, float]:
    """The option a label is, and how much of its wording it shows.

    The label is matched word by word against each option's stable wording, in
    order, with words the redraw broke or the width cut allowed to be missing,
    and it has to start as the option does: its first word, whole or broken. A
    label that ends in an ellipsis was cut, and only the words before the cut
    count. The best match has to be clearly the best.
    """
    label = clean_label(label)
    seen = words(label)
    cut = label.endswith(ELLIPSES)
    considered = seen[:-1] if cut else seen
    if not considered:
        return UNKNOWN, 0.0
    scores: list[tuple[float, str]] = []
    for option in OPTIONS:
        wanted = words(option.text)
        if option.whole:
            scores.append((1.0 if seen == wanted and not cut else 0.0, option.kind))
            continue
        anchored = wanted[0] in considered[:2] or wanted[0].startswith(considered[0])
        found = in_order(considered, wanted)
        enough = anchored and found >= min(2, len(wanted))
        base = min(len(wanted), len(considered)) if cut else len(wanted)
        scores.append((found / base if enough and base else 0.0, option.kind))
    scores.sort(reverse=True)
    (best, kind), (second, _) = scores[0], scores[1]
    return (kind, best) if best >= MATCH and best > second else (UNKNOWN, 0.0)


def foreign(lines: Iterable[str], known: frozenset[str]) -> bool:
    """Whether these lines draw a word the menu does not have: something else is on screen.

    A fragment of one of the menu's own words is the menu redrawn in part, and
    numbers and short words say nothing either way.
    """
    for line in lines:
        for word in words(line):
            if len(word) < MIN_FOREIGN or word.isdigit():
                continue
            if word in known or any(word in whole for whole in known):
                continue
            return True
    return False


def _lines(text: str) -> list[str]:
    """The screen text line by line, with the empty lines a move down leaves taken out."""
    return [line for line in text.split("\n") if line.strip()]


def _rows(lines: Sequence[str]) -> list[Row]:
    """The numbered rows among these lines, with a wrapped label joined to its row.

    A row is read by its own line first. The line after it, and the one after
    that, join it only while they make it more of an option it already is, or
    first make it one: the words the width wrapped, and never a line that says
    something else.
    """
    rows: list[Row] = []
    index = 0
    while index < len(lines):
        found = _ROW.match(lines[index])
        index += 1
        if found is None:
            continue
        label = clean_label(found.group(2))
        kind, score = _match(label)
        for extra in lines[index : index + 2]:
            if _ROW.match(extra) or is_footer(extra) or is_title(extra):
                break
            joined = f"{label} {clean_label(extra)}"
            joined_kind, joined_score = _match(joined)
            if joined_kind == UNKNOWN or joined_score <= score:
                break
            if kind not in (UNKNOWN, joined_kind):
                break
            label, kind, score = joined, joined_kind, joined_score
        rows.append(Row(number=int(found.group(1)), label=label, kind=kind))
    return rows


def find_menu(text: str) -> Menu | None:
    """The last usage-limit menu the screen text holds, whatever was drawn after it.

    From the last title that opens one: the rows after it, up to its footer.
    `Menu.last` says whether the menu is still the last thing drawn, which is
    what a menu just put up has to be; `read_menu` asks for both.
    """
    lines = _lines(text)
    titles = [index for index, line in enumerate(lines) if is_title(line)]
    for start in reversed(titles):
        end = next(
            (index for index in range(start + 1, len(lines)) if is_footer(lines[index])), None
        )
        if end is None:
            continue
        rows = _rows(lines[start + 1 : end])
        numbers = [row.number for row in rows]
        if not rows or numbers != sorted(set(numbers)):
            continue
        if not any(_BY_KIND[row.kind].only_here for row in rows if row.kind in _BY_KIND):
            continue
        return Menu(rows=tuple(rows), after=tuple(lines[end + 1 :]))
    return None


def read_menu(text: str) -> Menu | None:
    """The usage-limit menu, when it is what the screen shows now: nothing foreign after it."""
    menu = find_menu(text)
    return menu if menu is not None and menu.last else None


def shows_dialog(text: str) -> bool:
    """Whether a menu under this title or with this footer is the last thing drawn.

    What is said when the menu cannot be read: something with the usage-limit
    menu's title or footer is up, and nothing has been drawn over it since.
    """
    lines = _lines(text)
    marks = [index for index, line in enumerate(lines) if is_title(line) or is_footer(line)]
    if not marks:
        return False
    tail = lines[marks[-1] + 1 :]
    rows = [clean_label(found.group(2)) for line in tail if (found := _ROW.match(line))]
    return not foreign(tail, vocabulary(rows))


def whole(row: Row) -> bool:
    """Whether a known row shows every word of its option's wording, in order and unbroken.

    Only such a row is ever pressed: a key goes to words the device has read,
    never to a number or to a guess at what a broken word said.
    """
    option = _BY_KIND.get(row.kind)
    if option is None:
        return False
    wanted = words(option.text)
    return in_order(words(row.label), wanted) == len(wanted)


def card_label(row: Row) -> str:
    """The words an app shows for a row: the CLI's own, unless the redraw broke them.

    A label that shows every word of its option's wording, in order, is the
    CLI's text as it stands, the time a row names and a cut included. One the
    redraw or the width broke is shown as the option's own wording instead.
    """
    option = _BY_KIND.get(row.kind)
    if option is None:
        return row.label
    wanted = words(option.text)
    return row.label if in_order(words(row.label), wanted) == len(wanted) else option.text


def screen_lines(text: str) -> list[str]:
    """The screen text line by line, as everything here reads it."""
    return _lines(text)
