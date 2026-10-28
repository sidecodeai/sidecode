"""What the panel is told of a send: the same message from a press of Send and from a spoken yes."""

from __future__ import annotations

import asyncio
from pathlib import Path

from .fakes import FakeClock, until
from .harness import (
    Harness,
    confirm,
    function_call,
    person_says,
    prepared,
    read_back,
    start,
)
from .rig import MINE

SENT = {"type": "draft.result", "ok": True, "session": "Fix flaky auth test"}


def draft_traffic(h: Harness) -> list[str]:
    """What the panel heard about its draft, in order: the card (draft) and the outcome (result)."""
    return [m["type"] for m in h.browser.messages if m["type"] in ("draft", "draft.result")]


async def draft_on_the_panel(tmp_path: Path) -> tuple[Harness, str]:
    h = await start(tmp_path)
    await h.rig.publish(MINE, title="Fix flaky auth test", state="idle")
    h.upstream.push(function_call("prepare_message", {"session": "auth", "text": "Go on"}))
    await until(lambda: bool(h.browser.of("draft")))
    return h, h.browser.of("draft")[0]["draft"]["id"]


async def read_back_and_agree(h: Harness, clock: FakeClock) -> None:
    await read_back(h, clock)
    await person_says(h, "Yes.")


async def card_can_still_be_cleared(h: Harness, draft_id: str) -> None:
    """After a refusal the card is not held any more: cancelling it takes it off the panel."""
    shown = len(h.browser.of("draft"))
    h.browser.command(type="draft.cancel", draft_id=draft_id)
    await until(lambda: len(h.browser.of("draft")) == shown + 1)
    assert h.browser.of("draft")[-1] == {"type": "draft", "draft": None}


# ---- a spoken yes ----


async def test_a_spoken_yes_that_sends_the_draft_tells_the_panel_as_its_button_does(
    tmp_path: Path,
) -> None:
    clock = FakeClock()
    h, draft_id = await prepared(tmp_path, clock)
    await read_back_and_agree(h, clock)
    sending = asyncio.create_task(confirm(h, draft_id, "c2"))
    await h.rig.answer_device()
    assert (await sending)["sent"] is True
    await until(lambda: len(h.browser.of("draft")) == 2)
    # The outcome first, then the update that takes the card away.
    assert h.browser.of("draft.result") == [{**SENT, "draft_id": draft_id}]
    assert draft_traffic(h) == ["draft", "draft.result", "draft"]
    assert h.browser.of("draft")[1] == {"type": "draft", "draft": None}
    await h.stop()


async def test_a_spoken_send_the_gate_refuses_tells_the_panel_nothing(tmp_path: Path) -> None:
    clock = FakeClock()
    h, draft_id = await prepared(tmp_path, clock)
    assert (await confirm(h, draft_id, "c2"))["error"] == "not_read_back"
    await read_back(h, clock)
    assert (await confirm(h, draft_id, "c3"))["error"] == "not_confirmed"
    await h.upstream.settled()
    assert draft_traffic(h) == ["draft"]
    await card_can_still_be_cleared(h, draft_id)
    await h.stop()


async def test_a_spoken_send_the_computer_refuses_tells_the_panel_nothing_and_keeps_the_card(
    tmp_path: Path,
) -> None:
    clock = FakeClock()
    h, draft_id = await prepared(tmp_path, clock)
    await read_back_and_agree(h, clock)
    sending = asyncio.create_task(confirm(h, draft_id, "c2"))
    await h.rig.refuse_device()
    assert (await sending)["error"] == "send_failed"
    await h.upstream.settled()
    assert draft_traffic(h) == ["draft"]
    assert h.rig.drafts.pending() is not None
    await card_can_still_be_cleared(h, draft_id)
    await h.stop()


async def test_the_card_stays_while_the_computer_still_has_a_spoken_send(tmp_path: Path) -> None:
    clock = FakeClock()
    h, draft_id = await prepared(tmp_path, clock)
    await read_back_and_agree(h, clock)
    sending = asyncio.create_task(confirm(h, draft_id, "c2"))
    await h.rig.send_reached_device()
    # The watch loop ticks every 10 ms: many ticks go by while the computer has the message.
    await asyncio.sleep(0.15)
    assert draft_traffic(h) == ["draft"]
    await h.rig.answer_device()
    assert (await sending)["sent"] is True
    await until(lambda: len(h.browser.of("draft")) == 2)
    assert draft_traffic(h) == ["draft", "draft.result", "draft"]
    await h.stop()


# ---- the button ----


async def test_the_send_button_tells_the_panel_the_same_message(tmp_path: Path) -> None:
    h, draft_id = await draft_on_the_panel(tmp_path)
    h.browser.command(type="draft.send", draft_id=draft_id)
    await h.rig.answer_device()
    await until(lambda: len(h.browser.of("draft")) == 2)
    assert h.browser.of("draft.result") == [{**SENT, "draft_id": draft_id}]
    assert draft_traffic(h) == ["draft", "draft.result", "draft"]
    await h.stop()


async def test_the_card_stays_while_the_computer_still_has_a_sent_button_press(
    tmp_path: Path,
) -> None:
    h, draft_id = await draft_on_the_panel(tmp_path)
    h.browser.command(type="draft.send", draft_id=draft_id)
    await h.rig.send_reached_device()
    await asyncio.sleep(0.15)
    assert draft_traffic(h) == ["draft"]
    await h.rig.answer_device()
    await until(lambda: len(h.browser.of("draft")) == 2)
    assert draft_traffic(h) == ["draft", "draft.result", "draft"]
    await h.stop()


async def test_a_button_press_the_computer_refuses_says_so_and_keeps_the_card(
    tmp_path: Path,
) -> None:
    h, draft_id = await draft_on_the_panel(tmp_path)
    h.browser.command(type="draft.send", draft_id=draft_id)
    await h.rig.refuse_device()
    await until(lambda: bool(h.browser.of("draft.result")))
    await h.upstream.settled()
    assert h.browser.of("draft.result") == [
        {"type": "draft.result", "draft_id": draft_id, "ok": False, "error": "send_failed"}
    ]
    # The card never went away, so there is nothing to put back.
    assert draft_traffic(h) == ["draft", "draft.result"]
    assert h.rig.drafts.pending() is not None
    await card_can_still_be_cleared(h, draft_id)
    await h.stop()
