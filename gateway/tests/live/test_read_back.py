"""What counts as a read-back: two seconds of the assistant's voice, and not a remark."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from rc_gateway.live.gate import READBACK_SECONDS

from .fakes import FakeClock, pcm_ms, until
from .harness import (
    audio,
    confirm,
    function_call,
    person_says,
    prepared,
    read_back,
)


@pytest.mark.parametrize(
    ("milliseconds", "armed"),
    [(1500, False), (1900, False), (2000, True), (2200, True), (6000, True)],
)
async def test_a_read_back_arms_the_gate_from_two_seconds_of_voice(
    tmp_path: Path, milliseconds: int, armed: bool
) -> None:
    assert READBACK_SECONDS == 2.0
    h, draft_id = await prepared(tmp_path, FakeClock())
    h.upstream.push(audio(pcm_ms(milliseconds)))
    await h.upstream.settled()
    refusal = (await confirm(h, draft_id, "c2"))["error"]
    assert refusal == ("not_confirmed" if armed else "not_read_back")
    await h.stop()


async def test_a_chunk_with_a_little_voice_in_a_lot_of_silence_counts_only_the_voice(
    tmp_path: Path,
) -> None:
    h, draft_id = await prepared(tmp_path, FakeClock())

    def chunk() -> None:
        # About half a second of voice (twelve frames) inside four seconds of silence.
        h.upstream.push(audio(pcm_ms(2000, level=0) + pcm_ms(480) + pcm_ms(2000, level=0)))

    chunk()
    await h.upstream.settled()
    assert h.rig.gate.voiced_seconds == pytest.approx(0.48)
    assert (await confirm(h, draft_id, "c2"))["error"] == "not_read_back"
    # Four more such chunks, and the voice in them comes to more than a read-back needs.
    for _ in range(4):
        chunk()
    await h.upstream.settled()
    assert h.rig.gate.voiced_seconds == pytest.approx(2.4)
    assert (await confirm(h, draft_id, "c3"))["error"] == "not_confirmed"
    await h.stop()


async def test_the_digital_silence_the_service_streams_is_not_a_read_back(tmp_path: Path) -> None:
    h, draft_id = await prepared(tmp_path, FakeClock())
    h.upstream.push(audio(pcm_ms(5000, level=0)))
    await h.upstream.settled()
    assert (await confirm(h, draft_id, "c2"))["error"] == "not_read_back"
    h.upstream.push(audio(pcm_ms(1000)))  # a second of voice in the middle of the silence
    await h.upstream.settled()
    assert (await confirm(h, draft_id, "c3"))["error"] == "not_read_back"
    await h.stop()


async def test_nothing_said_after_an_interjection_arms_the_spoiled_draft_however_long(
    tmp_path: Path,
) -> None:
    clock = FakeClock()
    h, draft_id = await prepared(tmp_path, clock)
    h.upstream.push(audio(pcm_ms(1500)))  # the read-back begins
    await until(lambda: h.browser.audio_ms() >= 1500)
    await person_says(h, "Wait, no, forget it.")  # and the person cuts in
    assert h.browser.of("flush") != []
    clock.advance(2.0)
    h.upstream.push(audio(pcm_ms(1200)))  # "Let me check."
    await until(lambda: h.browser.audio_ms() >= 1500 + 1200)
    clock.advance(3.0)
    assert (await confirm(h, draft_id, "c2"))["error"] == "read_back_interrupted"
    h.upstream.push(audio(pcm_ms(1200)))  # the backend's spoken answer: 2.4 s in all
    await h.upstream.settled()
    clock.advance(4.0)
    await person_says(h, "Thanks.")  # anything at all
    assert (await confirm(h, draft_id, "c3"))["error"] == "read_back_interrupted"
    await h.stop()


async def test_a_short_read_back_that_was_refused_and_is_read_again_arms_the_gate(
    tmp_path: Path,
) -> None:
    clock = FakeClock()
    h, draft_id = await prepared(tmp_path, clock)
    h.upstream.push(audio(pcm_ms(1200)))
    await h.upstream.settled()
    assert (await confirm(h, draft_id, "c2"))["error"] == "not_read_back"
    # Read again, as the refusal tells the model to do.
    h.upstream.push(audio(pcm_ms(1200)))
    await h.upstream.settled()
    assert (await confirm(h, draft_id, "c3"))["error"] == "not_confirmed"
    clock.advance(1.0)
    await until(lambda: h.browser.audio_ms() >= 2400)
    clock.advance(4.0)
    await person_says(h, "Yes.")
    sending = asyncio.create_task(confirm(h, draft_id, "c4"))
    await h.rig.answer_device()
    assert (await sending)["sent"] is True
    await h.stop()


async def test_a_new_draft_read_back_in_full_is_confirmed_after_an_interjection(
    tmp_path: Path,
) -> None:
    clock = FakeClock()
    h, draft_id = await prepared(tmp_path, clock)
    h.upstream.push(audio(pcm_ms(1500)))
    await until(lambda: h.browser.audio_ms() >= 1500)
    await person_says(h, "Wait, no, tell it something else.")
    clock.advance(2.0)
    await read_back(h, clock)
    assert (await confirm(h, draft_id, "c2"))["error"] == "read_back_interrupted"
    # What the refusal tells the model to do: prepare the message again, and read it back in full.
    h.upstream.push(function_call("prepare_message", {"session": "auth", "text": "Else"}, "c3"))
    await until(lambda: len(h.browser.of("draft")) >= 2)
    new_id = h.browser.of("draft")[-1]["draft"]["id"]
    assert (await confirm(h, new_id, "c4"))["error"] == "not_read_back"
    await read_back(h, clock)
    await person_says(h, "Yes.")
    sending = asyncio.create_task(confirm(h, new_id, "c5"))
    request = await h.rig.answer_device()
    assert request["text"] == "Else"
    assert (await sending)["sent"] is True
    await h.stop()


async def test_a_second_draft_needs_its_own_two_seconds(tmp_path: Path) -> None:
    clock = FakeClock()
    h, _first = await prepared(tmp_path, clock)
    h.upstream.push(audio(pcm_ms(1500)))  # most of a read-back of the first draft
    await h.upstream.settled()
    h.upstream.push(function_call("prepare_message", {"session": "auth", "text": "Another"}, "c2"))
    await until(lambda: len(h.browser.of("draft")) >= 2)
    second = h.browser.of("draft")[-1]["draft"]["id"]
    h.upstream.push(audio(pcm_ms(1500)))  # 3 s of voice in all, but only 1.5 s since this draft
    await h.upstream.settled()
    assert (await confirm(h, second, "c3"))["error"] == "not_read_back"
    h.upstream.push(audio(pcm_ms(600)))
    await h.upstream.settled()
    assert (await confirm(h, second, "c4"))["error"] == "not_confirmed"
    await h.stop()
