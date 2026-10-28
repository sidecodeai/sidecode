"""A call over in-memory ends: the relay, interruption, drafts, the limits, and every way out."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from pathlib import Path
from typing import Any

import pytest

from rc_gateway.live.config import LiveConfig
from rc_gateway.live.errors import LiveError
from rc_gateway.live.playback import LEAD_SECONDS, PLAYOUT_DELAY_SECONDS

from .fakes import LOUD, QUIET, FakeBrowser, FakeClock, FakeUpstream, pcm_ms, until
from .harness import (
    PATIENT,
    READ_BACK_SECONDS,
    Harness,
    b64,
    confirm,
    first,
    function_call,
    person_says,
    prepared,
    read_back,
    read_back_sent,
    start,
    usage_records,
)
from .rig import MINE

# ---- the relay ----


async def test_the_browser_is_told_the_call_is_ready_before_anything_else(tmp_path: Path) -> None:
    h = await start(tmp_path)
    await until(lambda: bool(h.browser.messages))
    ready = h.browser.messages[0]
    assert ready["type"] == "ready"
    assert ready["sample_rate"] == 24000
    assert ready["frame_ms"] == 20
    assert 595 <= ready["seconds_left"] <= 600
    await h.stop()


async def test_microphone_frames_go_up_as_base64_appends_in_order(tmp_path: Path) -> None:
    h = await start(tmp_path)
    frames = [QUIET, LOUD, QUIET]
    for frame in frames:
        h.browser.mic(frame)
    await until(lambda: len(h.upstream.of("session.input_audio.append")) == 3)
    sent = [base64.b64decode(e["audio"]) for e in h.upstream.of("session.input_audio.append")]
    assert sent == frames
    await h.stop()


async def test_assistant_audio_reaches_the_browser_paced_in_small_frames(tmp_path: Path) -> None:
    h = await start(tmp_path)
    h.upstream.push({"type": "session.output_audio.delta", "delta": b64(pcm_ms(2000))})
    await until(lambda: h.browser.audio_ms() > 0)
    # Two seconds of audio are not all handed over at once: only the lead goes ahead.
    assert h.browser.audio_ms() <= (LEAD_SECONDS + 0.2) * 1000
    await until(lambda: h.browser.audio_ms() == 2000, timeout=3)
    assert all(len(frame) <= 1920 for frame in h.browser.audio)
    assert b"".join(h.browser.audio) == pcm_ms(2000)
    await h.stop()


async def test_a_continuous_stream_of_silence_is_sent_but_is_not_the_assistant_speaking(
    tmp_path: Path,
) -> None:
    h = await start(tmp_path)
    for _ in range(5):
        h.upstream.push({"type": "session.output_audio.delta", "delta": b64(pcm_ms(100, 0))})
    await until(lambda: h.browser.audio_ms() == 500, timeout=3)
    assert h.browser.of("state") == []
    assert not h.call._playback.audible()
    await h.stop()


async def test_a_stream_of_silence_does_not_keep_the_call_from_going_idle(tmp_path: Path) -> None:
    clock = FakeClock()
    h = await start(tmp_path, clock=clock)
    clock.advance(59)
    h.upstream.push({"type": "session.output_audio.delta", "delta": b64(pcm_ms(100, 0))})
    await until(lambda: h.browser.audio_ms() > 0)
    clock.advance(2)
    await h.finished()
    assert h.browser.ended == "idle"


async def test_the_person_speaking_over_silence_interrupts_nothing(tmp_path: Path) -> None:
    h = await start(tmp_path)
    h.upstream.push({"type": "session.output_audio.delta", "delta": b64(pcm_ms(300, 0))})
    await until(lambda: h.browser.audio_ms() > 0)
    for _ in range(8):
        h.browser.mic(LOUD)
    await until(lambda: len(h.upstream.of("session.input_audio.append")) == 8)
    assert h.browser.of("flush") == []
    assert h.call._playback.interruptions == 0
    await h.stop()


async def test_voice_in_the_stream_shows_as_the_assistant_speaking(tmp_path: Path) -> None:
    h = await start(tmp_path)
    h.upstream.push({"type": "session.output_audio.delta", "delta": b64(pcm_ms(200))})
    await until(lambda: h.browser.audio_ms() > 0)
    h.browser.mic(QUIET)
    await until(lambda: [m["speaker"] for m in h.browser.of("state")] == ["assistant"])
    await h.stop()


async def test_state_follows_who_is_speaking(tmp_path: Path) -> None:
    h = await start(tmp_path)
    for _ in range(8):
        h.browser.mic(LOUD)
    await until(lambda: "user" in [m["speaker"] for m in h.browser.of("state")])
    await h.stop()


async def test_audio_that_is_not_base64_is_ignored(tmp_path: Path) -> None:
    h = await start(tmp_path)
    h.upstream.push({"type": "session.output_audio.delta", "delta": "!!!not base64!!!"})
    h.upstream.push({"type": "session.output_audio.delta", "delta": 42})
    h.upstream.push({"type": "session.output_audio.delta", "delta": b64(pcm_ms(40))})
    await until(lambda: h.browser.audio_ms() == 40)
    assert h.call.active
    await h.stop()


async def test_captions_are_sent_only_after_the_browser_asks(tmp_path: Path) -> None:
    h = await start(tmp_path)
    h.upstream.push({"type": "session.output_transcript.delta", "delta": "Two sessions."})
    h.upstream.push({"type": "session.input_transcript.delta", "delta": "What is running?"})
    await h.upstream.settled()
    assert h.browser.of("caption") == []

    h.browser.command(type="captions", enabled=True)
    await until(lambda: h.call._captions)
    h.upstream.push({"type": "session.output_transcript.delta", "delta": "Nothing waits."})
    h.upstream.push({"type": "session.input_transcript.delta", "delta": "Thanks."})
    await until(lambda: len(h.browser.of("caption")) == 2)
    assert h.browser.of("caption") == [
        {"type": "caption", "speaker": "assistant", "delta": "Nothing waits."},
        {"type": "caption", "speaker": "user", "delta": "Thanks."},
    ]
    h.browser.command(type="captions", enabled=False)
    await until(lambda: not h.call._captions)
    await h.stop()


async def test_a_function_call_is_answered_with_its_output_and_a_continue(tmp_path: Path) -> None:
    h = await start(tmp_path)
    await h.rig.publish(MINE, title="Fix flaky auth test", state="running")
    h.upstream.push(function_call("list_sessions", {}))
    await until(lambda: "response.create" in h.upstream.types())
    first, second = h.upstream.sent[0], h.upstream.sent[1]
    assert first["type"] == "response.item.create"
    assert first["item"]["type"] == "function_call_output"
    assert first["item"]["call_id"] == "call_1"
    assert json.loads(first["item"]["output"])["sessions"][0]["id"] == MINE
    assert second == {"type": "response.create"}
    await h.stop()


async def test_a_function_call_the_service_repeats_is_answered_once(tmp_path: Path) -> None:
    h = await start(tmp_path)
    await h.rig.publish(MINE, title="Fix flaky auth test", state="running")
    h.upstream.push(function_call("list_sessions", {}, "call_same"))
    h.upstream.push(function_call("list_sessions", {}, "call_same"))
    h.upstream.push(function_call("list_sessions", {}, "call_other"))
    await until(lambda: h.upstream.types().count("response.create") == 2)
    await asyncio.sleep(0.05)
    assert h.upstream.types().count("response.create") == 2
    outputs = h.upstream.of("response.item.create")
    assert sorted(o["item"]["call_id"] for o in outputs) == ["call_other", "call_same"]
    await h.stop()


async def test_a_tool_failure_is_the_models_to_report_not_the_calls_end(tmp_path: Path) -> None:
    h = await start(tmp_path)
    h.upstream.push(function_call("delete_everything", {}))
    await until(lambda: "response.create" in h.upstream.types())
    assert json.loads(h.upstream.sent[0]["item"]["output"])["error"] == "unknown_tool"
    assert h.call.active
    await h.stop()


async def test_service_errors_are_logged_without_their_text_and_do_not_end_the_call(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    h = await start(tmp_path)
    with caplog.at_level(logging.WARNING, logger="rc_gateway.live.call"):
        h.upstream.push(
            {
                "type": "error",
                "error": {"code": "bad", "type": "invalid_request_error", "message": "SECRET TEXT"},
            }
        )
        await until(lambda: any(r.getMessage() == "live service error" for r in caplog.records))
    assert h.call.active
    assert "SECRET TEXT" not in repr([r.__dict__ for r in caplog.records])
    assert h.browser.of("notice") == [{"type": "notice", "code": "bad"}]
    assert "SECRET TEXT" not in json.dumps(h.browser.messages)
    await h.stop()


# ---- drafts ----


async def test_a_draft_the_model_prepares_reaches_the_browser_and_can_be_cancelled(
    tmp_path: Path,
) -> None:
    h = await start(tmp_path)
    await h.rig.publish(MINE, title="Fix flaky auth test", state="idle")
    h.upstream.push(function_call("prepare_message", {"session": "auth", "text": "Go on"}))
    await until(lambda: bool(h.browser.of("draft")))
    draft = h.browser.of("draft")[0]["draft"]
    assert draft["text"] == "Go on"
    assert draft["session"] == "Fix flaky auth test"

    h.browser.command(type="draft.cancel", draft_id=draft["id"])
    await until(lambda: len(h.browser.of("draft")) == 2)
    assert h.browser.of("draft")[1] == {"type": "draft", "draft": None}
    thinking = h.upstream.of("session.thinking.append")[0]
    assert thinking["delegation_id"] is None
    assert "Cancel button" in thinking["content"]
    assert h.rig.drafts.pending() is None
    await h.stop()


async def test_the_send_button_sends_the_draft_and_tells_the_model(tmp_path: Path) -> None:
    h = await start(tmp_path)
    await h.rig.publish(MINE, title="Fix flaky auth test", state="idle")
    h.upstream.push(function_call("prepare_message", {"session": "auth", "text": "Go on"}))
    await until(lambda: bool(h.browser.of("draft")))
    draft_id = h.browser.of("draft")[0]["draft"]["id"]

    h.browser.command(type="draft.send", draft_id=draft_id)
    request = await h.rig.answer_device()
    assert request["text"] == "Go on"
    await until(lambda: bool(h.browser.of("draft.result")))
    assert h.browser.of("draft.result")[0] == {
        "type": "draft.result",
        "draft_id": draft_id,
        "ok": True,
        "session": "Fix flaky auth test",
    }
    await until(lambda: len(h.browser.of("draft")) == 2)
    assert h.browser.of("draft")[1]["draft"] is None
    assert "Send button" in h.upstream.of("session.thinking.append")[0]["content"]
    await h.stop()


async def test_a_button_for_a_draft_that_is_gone_says_so(tmp_path: Path) -> None:
    h = await start(tmp_path)
    h.browser.command(type="draft.send", draft_id="nope")
    await until(lambda: bool(h.browser.of("draft.result")))
    assert h.browser.of("draft.result")[0]["ok"] is False
    assert h.browser.of("draft.result")[0]["error"] == "no_such_draft"
    assert h.upstream.of("session.thinking.append") == []
    await h.stop()


async def test_a_draft_that_expires_disappears_from_the_panel(tmp_path: Path) -> None:
    clock = FakeClock()
    patient = LiveConfig(api_key="k", idle_seconds=10_000, max_call_seconds=100_000)
    h = await start(tmp_path, clock=clock, config=patient)
    h.rig.drafts._clock = clock
    await h.rig.publish(MINE, title="Fix flaky auth test", state="idle")
    h.upstream.push(function_call("prepare_message", {"session": "auth", "text": "Go on"}))
    await until(lambda: bool(h.browser.of("draft")))
    clock.advance(200)
    await until(lambda: len(h.browser.of("draft")) == 2)
    assert h.browser.of("draft")[1]["draft"] is None
    await h.stop()


async def test_the_spoken_yes_needs_the_readback_and_an_answer_to_it(tmp_path: Path) -> None:
    clock = FakeClock()
    h, draft_id = await prepared(tmp_path, clock)

    assert (await confirm(h, draft_id, "c2"))["error"] == "not_read_back"
    await read_back(h, clock)
    assert (await confirm(h, draft_id, "c3"))["error"] == "not_confirmed"

    await person_says(h, "Yes.")
    sending = asyncio.create_task(confirm(h, draft_id, "c4"))
    request = await h.rig.answer_device()
    assert request["text"] == "Go on"
    assert (await sending)["sent"] is True
    await h.stop()


async def test_an_acknowledgement_spoken_before_the_draft_does_not_arm_the_gate(
    tmp_path: Path,
) -> None:
    clock = FakeClock()
    patient = LiveConfig(api_key="k", idle_seconds=10_000, max_call_seconds=100_000)
    h = await start(tmp_path, clock=clock, config=patient)
    await h.rig.publish(MINE, title="Fix flaky auth test", state="idle")
    # "Let me check": the delegation acknowledgement, voice and words, before the tool ran.
    h.upstream.push({"type": "session.output_transcript.delta", "delta": "Let me check."})
    h.upstream.push({"type": "session.output_audio.delta", "delta": b64(pcm_ms(200))})
    await until(lambda: h.browser.audio_ms() >= 200)
    h.upstream.push(function_call("prepare_message", {"session": "auth", "text": "Go on"}, "c1"))
    await until(lambda: bool(h.browser.of("draft")))
    draft_id = h.browser.of("draft")[0]["draft"]["id"]
    clock.advance(2)

    await person_says(h, "Yes, do it.")
    assert (await confirm(h, draft_id, "c2"))["error"] == "not_read_back"
    await h.stop()


async def test_a_late_fragment_of_the_original_request_is_not_an_answer(tmp_path: Path) -> None:
    clock = FakeClock()
    h, draft_id = await prepared(tmp_path, clock)
    await read_back(h, clock)
    # The last words of what the person asked for are transcribed only now: nobody spoke.
    h.upstream.push({"type": "session.input_transcript.delta", "delta": "...and tell it to go"})
    await h.upstream.settled()
    assert (await confirm(h, draft_id, "c2"))["error"] == "not_confirmed"
    await h.stop()


async def test_an_interjection_during_the_read_back_spoils_the_draft_for_voice(
    tmp_path: Path,
) -> None:
    clock = FakeClock()
    h, draft_id = await prepared(tmp_path, clock)
    # The read-back is being said when the person talks over it.
    h.upstream.push({"type": "session.output_audio.delta", "delta": b64(pcm_ms(200))})
    await until(lambda: h.browser.audio_ms() >= 200)
    await person_says(h, "Wait, no, tell it something else.")
    assert h.browser.of("flush") != []
    assert (await confirm(h, draft_id, "c2"))["error"] == "read_back_interrupted"

    # However well the draft is read back again, and however clearly they then say yes, it stays so.
    clock.advance(2)
    await read_back(h, clock)
    await person_says(h, "Yes.")
    assert (await confirm(h, draft_id, "c3"))["error"] == "read_back_interrupted"
    await h.stop()


async def test_echo_or_noise_during_the_read_back_is_an_interjection_that_spoils_the_draft(
    tmp_path: Path,
) -> None:
    clock = FakeClock()
    h, draft_id = await prepared(tmp_path, clock)
    # Loud audio on the microphone while the read-back plays, and a transcript of it.
    h.upstream.push({"type": "session.output_audio.delta", "delta": b64(pcm_ms(200))})
    await until(lambda: h.browser.audio_ms() >= 200)
    await person_says(h, "the assistant's own words, heard again")
    clock.advance(2)
    assert (await confirm(h, draft_id, "c2"))["error"] == "read_back_interrupted"
    await h.stop()


async def test_a_transcript_of_noise_after_a_read_back_that_finished_is_not_an_answer(
    tmp_path: Path,
) -> None:
    clock = FakeClock()
    h, draft_id = await prepared(tmp_path, clock)
    await read_back(h, clock)
    h.upstream.push({"type": "session.input_transcript.delta", "delta": "uh"})
    await h.upstream.settled()
    assert (await confirm(h, draft_id, "c2"))["error"] == "not_confirmed"
    await h.stop()


@pytest.mark.parametrize(
    ("silence", "answers"),
    [(0.1, False), (0.3, False), (0.45, False), (0.55, True), (0.8, True), (5.0, True)],
)
async def test_the_persons_voice_answers_only_after_half_a_second_of_silence(
    tmp_path: Path, silence: float, answers: bool
) -> None:
    clock = FakeClock()
    h, draft_id = await prepared(tmp_path, clock)
    await read_back_sent(h, clock)
    # The read-back was sent, is held by the browser for its playout delay and then played: it
    # finishes being heard `silence` seconds before the person speaks. Sooner than half a second
    # after it, they are talking over it, and that spoils the draft for voice confirmation.
    clock.advance(READ_BACK_SECONDS - 1.0 + PLAYOUT_DELAY_SECONDS + silence)
    await person_says(h, "Yes.")
    if answers:
        sending = asyncio.create_task(confirm(h, draft_id, "c2"))
        await h.rig.answer_device()
        assert (await sending)["sent"] is True
    else:
        assert (await confirm(h, draft_id, "c2"))["error"] == "read_back_interrupted"
    await h.stop()


async def test_a_sound_in_a_pause_between_the_sentences_of_the_read_back_spoils_the_draft(
    tmp_path: Path,
) -> None:
    clock = FakeClock()
    h, draft_id = await prepared(tmp_path, clock)
    # The first sentence, then a pause of 0.4 s in which the person coughs and the recogniser
    # writes it down.
    h.upstream.push({"type": "session.output_audio.delta", "delta": b64(pcm_ms(200))})
    await until(lambda: h.browser.audio_ms() >= 200)
    clock.advance(0.6)
    await person_says(h, "Mm.")
    # The cough is over and the second sentence follows, and then the person's yes. Neither is
    # taken for the answer to a read-back: the cough was an interjection.
    clock.advance(0.5)
    await read_back(h, clock)
    assert (await confirm(h, draft_id, "c2"))["error"] == "read_back_interrupted"
    await person_says(h, "Yes.")
    assert (await confirm(h, draft_id, "c3"))["error"] == "read_back_interrupted"
    await h.stop()


async def test_the_button_needs_none_of_this_and_the_cancel_button_forgets_the_answer(
    tmp_path: Path,
) -> None:
    clock = FakeClock()
    h, draft_id = await prepared(tmp_path, clock)
    await read_back(h, clock)
    await person_says(h, "Yes.")
    h.browser.command(type="draft.cancel", draft_id=draft_id)
    await until(lambda: h.rig.drafts.pending() is None)
    h.upstream.push(function_call("prepare_message", {"session": "auth", "text": "Again"}, "c3"))
    await until(lambda: len(h.browser.of("draft")) >= 3)
    new_id = h.browser.of("draft")[-1]["draft"]["id"]
    assert (await confirm(h, new_id, "c4"))["error"] == "not_read_back"
    await h.stop()


# ---- interruption ----


async def test_the_person_talking_over_the_assistant_flushes_and_silences_it(
    tmp_path: Path,
) -> None:
    h = await start(tmp_path)
    h.upstream.push({"type": "session.output_audio.delta", "delta": b64(pcm_ms(3000))})
    await until(lambda: h.browser.audio_ms() > 0)

    for _ in range(8):
        h.browser.mic(LOUD)
    await until(lambda: bool(h.browser.of("flush")))
    assert len(h.browser.of("flush")) == 1
    heard = h.browser.audio_ms()
    assert heard <= (LEAD_SECONDS + 0.2) * 1000

    # What the model says while the person is still talking is not sent.
    h.upstream.push({"type": "session.output_audio.delta", "delta": b64(pcm_ms(500))})
    await asyncio.sleep(0.1)
    assert h.browser.audio_ms() == heard
    await h.stop()


async def test_the_assistant_is_heard_again_once_the_person_stops(tmp_path: Path) -> None:
    clock = FakeClock()
    h = await start(tmp_path, clock=clock)
    h.upstream.push({"type": "session.output_audio.delta", "delta": b64(pcm_ms(2000))})
    await until(lambda: h.browser.audio_ms() > 0)
    for _ in range(8):
        h.browser.mic(LOUD)
    await until(lambda: bool(h.browser.of("flush")))

    clock.advance(1.0)
    h.browser.mic(QUIET)
    await until(lambda: len(h.upstream.of("session.input_audio.append")) == 9)
    h.upstream.push({"type": "session.output_audio.delta", "delta": b64(pcm_ms(40))})
    await until(lambda: h.call._playback.audible())
    await h.stop()


async def test_speech_while_the_assistant_is_quiet_interrupts_nothing(tmp_path: Path) -> None:
    h = await start(tmp_path)
    for _ in range(8):
        h.browser.mic(LOUD)
    await until(lambda: len(h.upstream.of("session.input_audio.append")) == 8)
    assert h.browser.of("flush") == []
    await h.stop()


async def test_with_barge_in_off_the_assistant_is_never_cut_off(tmp_path: Path) -> None:
    config = LiveConfig(api_key="k", barge_in=False)
    h = await start(tmp_path, config=config)
    h.upstream.push({"type": "session.output_audio.delta", "delta": b64(pcm_ms(1000))})
    await until(lambda: h.browser.audio_ms() > 0)
    for _ in range(8):
        h.browser.mic(LOUD)
    await until(lambda: len(h.upstream.of("session.input_audio.append")) == 8)
    assert h.browser.of("flush") == []
    await until(lambda: h.browser.audio_ms() == 1000, timeout=3)
    await h.stop()


# ---- the limits ----


async def test_silence_ends_the_call_after_the_idle_limit(tmp_path: Path) -> None:
    clock = FakeClock()
    h = await start(tmp_path, clock=clock)
    clock.advance(30)
    await asyncio.sleep(0.05)
    assert h.call.active
    clock.advance(31)
    await h.finished()
    assert h.browser.ended == "idle"


async def test_speaking_keeps_the_call_alive(tmp_path: Path) -> None:
    clock = FakeClock()
    h = await start(tmp_path, clock=clock)
    clock.advance(50)
    for _ in range(8):
        h.browser.mic(LOUD)
    await until(lambda: len(h.upstream.of("session.input_audio.append")) == 8)
    clock.advance(50)
    h.browser.mic(QUIET)
    await until(lambda: len(h.upstream.of("session.input_audio.append")) == 9)
    await asyncio.sleep(0.05)
    assert h.call.active
    clock.advance(11)
    await h.finished()
    assert h.browser.ended == "idle"


async def test_a_running_tool_holds_off_the_idle_hang_up(tmp_path: Path) -> None:
    clock = FakeClock()
    h = await start(tmp_path, clock=clock)
    gate = asyncio.Event()
    original = h.rig.tools.run

    async def slow(name: str, raw: str) -> str:
        await gate.wait()
        return await original(name, raw)

    h.rig.tools.run = slow  # type: ignore[method-assign,assignment]
    h.upstream.push(function_call("list_sessions", {}))
    await asyncio.sleep(0.05)
    clock.advance(120)
    await asyncio.sleep(0.1)
    assert h.call.active
    gate.set()
    await until(lambda: "response.create" in h.upstream.types())
    await h.stop()


async def test_audio_the_assistant_is_still_saying_holds_off_the_idle_hang_up(
    tmp_path: Path,
) -> None:
    clock = FakeClock()
    h = await start(tmp_path, clock=clock)
    h.upstream.push({"type": "session.output_audio.delta", "delta": b64(pcm_ms(10_000))})
    await until(lambda: h.browser.audio_ms() > 0)
    clock.advance(61)
    await asyncio.sleep(0.1)
    assert h.call.active
    await h.stop()


async def test_the_hard_cap_ends_a_call_that_is_still_busy(tmp_path: Path) -> None:
    clock = FakeClock()
    h = await start(tmp_path, clock=clock, config=PATIENT)
    clock.advance(599)
    await asyncio.sleep(0.05)
    assert h.call.active
    clock.advance(2)
    await h.finished()
    assert h.browser.ended == "max_duration"


async def test_the_service_may_shorten_a_call_with_its_own_expiry(tmp_path: Path) -> None:
    clock = FakeClock()
    started = {"session": {"id": "s", "expires_at": 1_000_100}}
    h = await start(
        tmp_path, clock=clock, config=PATIENT, started=started, wall_clock=lambda: 1_000_000.0
    )
    ready = await first(h, "ready")
    assert ready["seconds_left"] == 100
    clock.advance(101)
    await h.finished()
    assert h.browser.ended == "max_duration"


async def test_an_expiry_that_is_already_past_is_not_a_limit(tmp_path: Path) -> None:
    started = {"session": {"id": "s", "expires_at": 5}}
    h = await start(tmp_path, started=started, wall_clock=lambda: 1_000_000.0)
    ready = await first(h, "ready")
    assert ready["seconds_left"] >= 595
    await h.stop()


async def test_time_left_is_sent_now_and_then(tmp_path: Path) -> None:
    clock = FakeClock()
    h = await start(tmp_path, clock=clock)
    clock.advance(4)
    await until(lambda: bool(h.browser.of("time")))
    assert h.browser.of("time")[0]["seconds_left"] == 596
    await h.stop()


# ---- every way out closes both ends ----


async def closed_both_ends(
    h: Harness, reason: str, *, asked: bool, caplog: pytest.LogCaptureFixture
) -> None:
    await h.finished()
    assert h.browser.ended == reason
    assert h.upstream.closed
    assert ("session.close" in h.upstream.types()) is asked
    assert h.ended == [h.call]
    [record] = usage_records(caplog)
    assert record["reason"] == reason
    assert record["account"] == "admin"
    assert record["call_id"] == h.call.call_id


async def test_the_browser_closing_closes_the_service_at_once(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="rc_gateway.live.call")
    h = await start(tmp_path)
    await until(lambda: bool(h.browser.messages))
    h.browser.close()
    await closed_both_ends(h, "browser_closed", asked=True, caplog=caplog)


async def test_a_hang_up_from_the_panel_closes_both_ends(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="rc_gateway.live.call")
    h = await start(tmp_path)
    h.browser.command(type="hangup")
    await closed_both_ends(h, "hangup", asked=True, caplog=caplog)


async def test_the_service_dropping_tells_the_browser_and_closes_it(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="rc_gateway.live.call")
    h = await start(tmp_path)
    await until(lambda: bool(h.browser.messages))
    h.upstream.drop()
    await closed_both_ends(h, "upstream_lost", asked=False, caplog=caplog)


@pytest.mark.parametrize(
    ("said", "reason"),
    [
        ("content", "content"),
        ("expired", "expired"),
        ("remote_hangup", "upstream_lost"),
        ("connection_lost", "upstream_lost"),
    ],
)
async def test_the_service_ending_the_session_is_passed_on(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, said: str, reason: str
) -> None:
    caplog.set_level(logging.INFO, logger="rc_gateway.live.call")
    h = await start(tmp_path)
    h.upstream.push({"type": "session.closed", "reason": said, "usage": {"seconds": 7}})
    await closed_both_ends(h, reason, asked=False, caplog=caplog)
    assert usage_records(caplog)[0]["voice_seconds"] == 7.0


async def test_the_gateway_stopping_closes_both_ends(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="rc_gateway.live.call")
    h = await start(tmp_path)
    await until(lambda: bool(h.browser.messages))
    await h.call.finish("gateway_stopping")
    await closed_both_ends(h, "gateway_stopping", asked=True, caplog=caplog)


async def test_a_cancelled_handler_still_closes_both_ends(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="rc_gateway.live.call")
    h = await start(tmp_path)
    await until(lambda: bool(h.browser.messages))
    h.task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await h.task
    await until(lambda: h.upstream.closed)
    assert h.browser.ended == "cancelled"
    assert "session.close" in h.upstream.types()
    assert h.ended == [h.call]
    assert len(usage_records(caplog)) == 1


@pytest.mark.parametrize(
    "frame", [b"\x00" * 3, b"\x00" * 100, b"\x00" * 9000], ids=["odd", "tiny", "huge"]
)
async def test_a_malformed_audio_frame_ends_the_call(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, frame: bytes
) -> None:
    caplog.set_level(logging.INFO, logger="rc_gateway.live.call")
    h = await start(tmp_path)
    h.browser.mic(frame)
    await closed_both_ends(h, "protocol_error", asked=True, caplog=caplog)


async def test_audio_faster_than_real_time_ends_the_call(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="rc_gateway.live.call")
    h = await start(tmp_path, clock=FakeClock())
    for _ in range(200):
        h.browser.mic(QUIET)
    await closed_both_ends(h, "protocol_error", asked=True, caplog=caplog)


async def test_a_browser_that_goes_silent_is_dropped(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="rc_gateway.live.call")
    h = await start(tmp_path, browser_silence=0.1)
    await closed_both_ends(h, "browser_silent", asked=True, caplog=caplog)


async def test_a_stuck_service_ends_the_call(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="rc_gateway.live.call")
    h = await start(tmp_path)
    await until(lambda: bool(h.browser.messages))
    h.upstream.send_error = "upstream_stalled"
    h.browser.mic(QUIET)
    await h.finished()
    assert h.browser.ended == "upstream_stalled"
    assert h.upstream.closed
    assert h.ended == [h.call]


async def test_a_browser_that_cannot_be_written_to_ends_the_call(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="rc_gateway.live.call")
    h = await start(tmp_path)
    await until(lambda: bool(h.browser.messages))
    h.browser.fail_sends = "browser_stalled"
    h.upstream.push({"type": "session.output_audio.delta", "delta": b64(pcm_ms(200))})
    await closed_both_ends(h, "browser_stalled", asked=True, caplog=caplog)


async def test_audio_backing_up_drops_the_oldest_and_the_call_goes_on(tmp_path: Path) -> None:
    h = await start(tmp_path)
    await until(lambda: bool(h.browser.messages))
    for _ in range(4):
        h.upstream.push({"type": "session.output_audio.delta", "delta": b64(pcm_ms(10_000))})
    await h.upstream.settled()
    assert h.call.active
    assert h.call._playback.pacer.dropped_bytes > 0
    assert h.call._playback.pacer.held_bytes <= 30 * 48_000
    await h.stop()


async def test_a_service_that_never_confirms_the_close_is_still_closed(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="rc_gateway.live.call")
    h = await start(tmp_path, upstream=FakeUpstream(answer_close=False))
    await until(lambda: bool(h.browser.messages))
    h.browser.command(type="hangup")
    await closed_both_ends(h, "hangup", asked=True, caplog=caplog)
    assert usage_records(caplog)[0]["usage_final"] is False


async def test_the_slot_is_released_before_the_service_confirms_the_close(
    tmp_path: Path,
) -> None:
    h = await start(tmp_path, upstream=FakeUpstream(answer_close=False))
    await until(lambda: bool(h.browser.messages))
    h.browser.command(type="hangup")
    await until(lambda: h.ended == [h.call])
    assert not h.upstream.closed
    await h.finished()
    assert h.upstream.closed


async def test_only_the_first_reason_is_kept(tmp_path: Path) -> None:
    h = await start(tmp_path)
    await until(lambda: bool(h.browser.messages))
    h.call.request_finish("idle")
    h.call.request_finish("hangup")
    await h.finished()
    assert h.browser.ended == "idle"
    assert h.upstream.types().count("session.close") == 1


async def test_nothing_is_sent_to_the_browser_or_service_after_the_end(tmp_path: Path) -> None:
    h = await start(tmp_path)
    await until(lambda: bool(h.browser.messages))
    h.browser.command(type="hangup")
    await h.finished()
    sent = len(h.upstream.sent)
    messages = len(h.browser.messages)
    h.upstream.push(function_call("list_sessions", {}))
    h.browser.mic(QUIET)
    await asyncio.sleep(0.05)
    assert len(h.upstream.sent) == sent
    assert len(h.browser.messages) == messages


# ---- an end that one side cannot hold up ----


class HangingBrowser(FakeBrowser):
    """A browser the gateway cannot finish saying goodbye to: its close waits on a peer that stopped
    reading (uvicorn's send waits for the socket to become writable)."""

    async def end(self, reason: str) -> None:
        self.ending = reason
        await asyncio.Event().wait()


class HangingUpstream(FakeUpstream):
    """A Live API socket whose graceful close never gets an answer."""

    async def send(self, event: dict[str, Any]) -> None:
        if event.get("type") == "session.close":
            await asyncio.Event().wait()
        await super().send(event)


async def test_a_browser_that_cannot_be_said_goodbye_to_does_not_hold_the_service_open(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="rc_gateway.live.call")
    h = await start(tmp_path, browser=HangingBrowser(), step_timeout=0.5)
    await until(lambda: bool(h.browser.messages))
    h.browser.command(type="hangup")
    # The Live API is told to close and is closed while the browser's farewell is still hanging.
    await until(lambda: h.upstream.closed, timeout=0.4)
    assert "session.close" in h.upstream.types()
    assert h.ended == [h.call]
    assert not h.task.done()
    await h.finished()
    assert len(usage_records(caplog)) == 1
    assert any(r.getMessage() == "live call step timed out" for r in caplog.records)


async def test_a_service_that_never_answers_the_close_does_not_hold_the_browser_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("rc_gateway.live.call.UPSTREAM_CLOSE_SLACK_SECONDS", 0.3)
    h = await start(tmp_path, upstream=HangingUpstream())
    await until(lambda: bool(h.browser.messages))
    h.browser.command(type="hangup")
    await until(lambda: h.browser.ended == "hangup", timeout=0.2)
    assert h.ended == [h.call]
    await h.finished()
    assert h.upstream.closed


async def test_when_both_ends_hang_the_call_still_ends_and_lets_go_of_its_slot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="rc_gateway.live.call")
    monkeypatch.setattr("rc_gateway.live.call.UPSTREAM_CLOSE_SLACK_SECONDS", 0.2)
    h = await start(
        tmp_path, browser=HangingBrowser(), upstream=HangingUpstream(), step_timeout=0.3
    )
    await until(lambda: bool(h.browser.messages))
    h.browser.command(type="hangup")
    await until(lambda: h.ended == [h.call], timeout=0.3)
    await h.finished()
    assert h.upstream.closed
    assert len(usage_records(caplog)) == 1


# ---- what a browser may ask of a call ----


async def test_commands_faster_than_a_person_sends_them_close_the_call(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="rc_gateway.live.call")
    h = await start(tmp_path, clock=FakeClock())
    for _ in range(40):
        h.browser.command(type="captions", enabled=True)
    await closed_both_ends(h, "protocol_error", asked=True, caplog=caplog)


async def test_a_few_commands_in_a_burst_are_a_person_pressing_buttons(tmp_path: Path) -> None:
    h = await start(tmp_path, clock=FakeClock())
    for _ in range(10):
        h.browser.command(type="captions", enabled=True)
    h.browser.mic(QUIET)
    await until(lambda: len(h.upstream.of("session.input_audio.append")) == 1)
    assert h.call.active
    await h.stop()


class StalledBrowser(FakeBrowser):
    """Reads fine, but every send to it hangs: a client that does not read what it is answered."""

    async def send_json(self, message: dict[str, Any]) -> None:
        if message.get("type") != "ready":
            await asyncio.Event().wait()
        await super().send_json(message)


async def test_a_browser_that_never_reads_cannot_pile_up_unfinished_commands(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="rc_gateway.live.call")
    h = await start(tmp_path, browser=StalledBrowser(), clock=FakeClock())
    await until(lambda: bool(h.browser.messages))
    for index in range(8):
        h.browser.command(type="draft.send", draft_id=f"{index:012d}")
    await h.finished()
    assert h.browser.ended == "protocol_error"
    assert h.upstream.closed
    assert len(h.call._commands) == 0


# ---- the Live API's errors ----


async def test_an_error_text_never_reaches_the_browser_only_a_code_that_looks_like_one(
    tmp_path: Path,
) -> None:
    h = await start(tmp_path)
    h.upstream.push(
        {"type": "error", "error": {"code": "rate_limit_exceeded", "message": "SECRET"}}
    )
    h.upstream.push(
        {"type": "error", "error": {"code": "The user said: SECRET words", "message": "x"}}
    )
    h.upstream.push({"type": "session.input_transcript.delta", "delta": "hello"})
    h.upstream.push({"type": "error", "error": "SECRET as text"})
    await until(lambda: len(h.browser.of("notice")) == 3)
    assert [n["code"] for n in h.browser.of("notice")] == [
        "rate_limit_exceeded",
        "unknown",
        "unknown",
    ]
    assert "SECRET" not in json.dumps(h.browser.messages)
    assert h.call.active
    await h.stop()


async def test_three_errors_in_a_row_end_the_call(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="rc_gateway.live.call")
    h = await start(tmp_path)
    for _ in range(3):
        h.upstream.push({"type": "error", "error": {"code": "invalid_value"}})
    await closed_both_ends(h, "upstream_errors", asked=True, caplog=caplog)
    assert len(h.browser.of("notice")) >= 1


async def test_errors_with_something_else_heard_between_them_do_not_add_up(
    tmp_path: Path,
) -> None:
    h = await start(tmp_path)
    for _ in range(6):
        h.upstream.push({"type": "error", "error": {"code": "invalid_value"}})
        h.upstream.push({"type": "session.usage.updated", "usage": {"seconds": 1}})
    await h.upstream.settled()
    assert h.call.active
    assert len(h.browser.of("notice")) == 6
    await h.stop()


# ---- the day's call time ----


async def test_the_days_remaining_time_shortens_the_call_and_ends_it_with_its_own_reason(
    tmp_path: Path,
) -> None:
    clock = FakeClock()
    h = await start(tmp_path, clock=clock, config=PATIENT, budget_seconds=30)
    ready = await first(h, "ready")
    assert ready["seconds_left"] == 30
    clock.advance(31)
    await h.finished()
    assert h.browser.ended == "daily_limit"


async def test_a_day_with_more_time_than_the_call_may_use_changes_nothing(tmp_path: Path) -> None:
    clock = FakeClock()
    h = await start(tmp_path, clock=clock, config=PATIENT, budget_seconds=5_000)
    assert (await first(h, "ready"))["seconds_left"] == 600
    clock.advance(601)
    await h.finished()
    assert h.browser.ended == "max_duration"


async def test_the_call_reports_how_long_it_lasted(tmp_path: Path) -> None:
    clock = FakeClock()
    h = await start(tmp_path, clock=clock, config=PATIENT)
    clock.advance(42)
    assert h.call.duration() == 42
    await h.stop()


# ---- the usage record ----


async def test_one_usage_line_carries_ids_seconds_and_what_openai_reported(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="rc_gateway.live.call")
    h = await start(tmp_path, started={"session": {"id": "live_sess_test"}})
    await until(lambda: bool(h.browser.messages))
    h.upstream.push({"type": "session.usage.updated", "usage": {"seconds": 5}})
    for response in ("resp_1", "resp_1", "resp_2"):
        h.upstream.push(
            {
                "type": "response.event",
                "event": {
                    "type": "response.completed",
                    "response": {
                        "id": response,
                        "output": [],
                        "usage": {
                            "input_tokens": 100,
                            "output_tokens": 10,
                            "input_tokens_details": {"cached_tokens": 30},
                            "output_tokens_details": {"reasoning_tokens": 2},
                        },
                    },
                },
            }
        )
    await until(lambda: h.call.usage.backend_responses == 2)
    h.browser.command(type="hangup")
    await h.finished()
    [record] = usage_records(caplog)
    assert set(record) == {
        "account",
        "call_id",
        "session_id",
        "reason",
        "seconds",
        "interruptions",
        "messages_sent",
        "voice_seconds",
        "usage_final",
        "backend_responses",
        "backend_input_tokens",
        "backend_output_tokens",
        "backend_cached_tokens",
        "backend_reasoning_tokens",
        "discarded_audio_ms",
        "dropped_audio_ms",
        "starved",
        "starved_ms",
        "starved_max_ms",
        "replies",
        "reply_ms_median",
        "reply_ms_max",
        "backend_ms_median",
        "backend_ms_max",
        "tool_ms_max",
        "browser_underruns",
        "browser_gap_ms",
        "browser_restarts",
        "browser_delay_ms",
        "speech_ms",
        "input_transcripts",
    }
    assert record["session_id"] == "live_sess_test"
    assert record["voice_seconds"] == 42.0  # the final figure the service sent on closing
    assert record["usage_final"] is True
    assert record["backend_responses"] == 2
    assert record["backend_input_tokens"] == 200
    assert record["backend_output_tokens"] == 20
    assert record["backend_cached_tokens"] == 60
    assert record["backend_reasoning_tokens"] == 4
    assert record["seconds"] >= 0
    # A call in which none of it happened says so in numbers: zero where something is counted, and
    # null where there is nothing to take a median or a latest value of.
    assert [record[name] for name in ("discarded_audio_ms", "dropped_audio_ms")] == [0, 0]
    assert [record[name] for name in ("starved", "starved_ms", "replies")] == [0, 0, 0]
    nothing = ("reply_ms_median", "reply_ms_max", "backend_ms_median", "backend_ms_max")
    assert [record[name] for name in nothing] == [None] * 4
    assert record["starved_max_ms"] is None
    assert record["tool_ms_max"] is None
    browser = ("browser_underruns", "browser_gap_ms", "browser_restarts", "browser_delay_ms")
    assert [record[name] for name in browser] == [None] * 4
    assert (record["speech_ms"], record["input_transcripts"]) == (0, 0)


async def test_the_usage_line_never_carries_what_was_said(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    h = await start(tmp_path)
    await h.rig.publish(MINE, title="Fix flaky auth test", state="idle")
    h.browser.command(type="captions", enabled=True)
    h.upstream.push({"type": "session.input_transcript.delta", "delta": "my password is hunter2"})
    h.upstream.push({"type": "session.output_transcript.delta", "delta": "purple elephant"})
    h.upstream.push(function_call("prepare_message", {"session": "auth", "text": "secret words"}))
    await until(lambda: bool(h.browser.of("draft")))
    h.browser.mic(LOUD)
    h.browser.command(type="hangup")
    await h.finished()
    logged = repr([(r.getMessage(), getattr(r, "fields", {})) for r in caplog.records])
    for word in ("hunter2", "purple elephant", "secret words", "Fix flaky auth test"):
        assert word not in logged


# ---- protocol hygiene ----


async def test_unknown_commands_are_ignored(tmp_path: Path) -> None:
    h = await start(tmp_path)
    h.browser.command(type="reboot")
    h.browser.command(type="draft.send")
    h.browser.command(type="draft.send", draft_id=7)
    h.browser.mic(QUIET)
    await until(lambda: len(h.upstream.of("session.input_audio.append")) == 1)
    assert h.call.active
    await h.stop()


async def test_a_protocol_error_from_the_browser_link_ends_the_call(tmp_path: Path) -> None:
    h = await start(tmp_path)
    h.browser._inbox.put_nowait(LiveError("protocol_error", "a control message is not JSON"))
    await h.finished()
    assert h.browser.ended == "protocol_error"
