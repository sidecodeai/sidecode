"""A call over in-memory ends, ready to drive: the harness the call tests share."""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from rc_gateway.live.barge_in import YIELD_SILENCE_SECONDS
from rc_gateway.live.call import LiveCall
from rc_gateway.live.config import LiveConfig
from rc_gateway.live.vad import OVER_ASSISTANT_ONSET_MS

from .fakes import (
    LOUD,
    QUIET,
    FakeBrowser,
    FakeClock,
    FakeUpstream,
    noise_floor_ms,
    pcm_ms,
    until,
)
from .rig import MINE, LiveRig, live_rig

CONFIG = LiveConfig(api_key="k", idle_seconds=60, max_call_seconds=600)
#: Never idle within the test: only the hard cap or the service's own expiry can end it.
PATIENT = LiveConfig(api_key="k", idle_seconds=10_000, max_call_seconds=600)
#: Loud microphone frames of 20 ms the person says something with: enough to begin speech while
#: the assistant is quiet (``ONSET_MS``), and not while it is audible.
SPEECH_FRAMES = 8
#: Enough to begin speech over the assistant too (``OVER_ASSISTANT_ONSET_MS``), and a little more.
CUT_IN_FRAMES = round(OVER_ASSISTANT_ONSET_MS / 20) + 1


@dataclass
class Harness:
    rig: LiveRig
    browser: FakeBrowser
    upstream: FakeUpstream
    call: LiveCall
    ended: list[LiveCall]
    task: asyncio.Task[None]

    async def finished(self) -> None:
        await asyncio.wait_for(self.task, 5)

    async def stop(self) -> None:
        await self.call.finish("hangup")
        await self.finished()


async def start(
    tmp_path: Path,
    *,
    config: LiveConfig = CONFIG,
    clock: Callable[[], float] | None = None,
    upstream: FakeUpstream | None = None,
    started: dict[str, Any] | None = None,
    wall_clock: Callable[[], float] | None = None,
    browser: FakeBrowser | None = None,
    **options: Any,
) -> Harness:
    rig = await live_rig(tmp_path)
    browser = browser if browser is not None else FakeBrowser()
    link = upstream if upstream is not None else FakeUpstream()
    ended: list[LiveCall] = []

    async def on_end(call: LiveCall) -> None:
        ended.append(call)

    extra: dict[str, Any] = {} if clock is None else {"clock": clock}
    if wall_clock is not None:
        extra["wall_clock"] = wall_clock
    call = LiveCall(
        username="admin",
        browser=browser,
        upstream=link,
        started=started if started is not None else {"session": {"id": "live_sess_test"}},
        config=config,
        tools=rig.tools,
        drafts=rig.drafts,
        gate=rig.gate,
        on_end=on_end,
        watch_interval=0.01,
        close_grace=0.3,
        **extra,
        **options,
    )
    return Harness(rig, browser, link, call, ended, asyncio.create_task(call.run()))


def function_call(name: str, arguments: dict[str, Any], call_id: str = "call_1") -> dict[str, Any]:
    return {
        "type": "response.event",
        "delegation_id": "item_1",
        "event": {
            "type": "response.output_item.done",
            "item": {
                "type": "function_call",
                "call_id": call_id,
                "name": name,
                "arguments": json.dumps(arguments),
            },
        },
    }


def usage_records(caplog: pytest.LogCaptureFixture) -> list[dict[str, Any]]:
    return [
        r.fields  # type: ignore[attr-defined]
        for r in caplog.records
        if r.getMessage() == "live call usage"
    ]


def backend_event(kind: str, response: str, **fields: Any) -> dict[str, Any]:
    """A nested Responses event of the backend that names its response."""
    return {
        "type": "response.event",
        "delegation_id": "item_1",
        "event": {"type": kind, "response": {"id": response, **fields}},
    }


def audio(pcm: bytes) -> dict[str, Any]:
    """The service's ``session.output_audio.delta`` for some PCM."""
    return {"type": "session.output_audio.delta", "delta": b64(pcm)}


def b64(pcm: bytes) -> str:
    return base64.b64encode(pcm).decode("ascii")


async def first(h: Harness, kind: str) -> dict[str, Any]:
    await until(lambda: bool(h.browser.of(kind)))
    return h.browser.of(kind)[0]


async def prepared(tmp_path: Path, clock: FakeClock) -> tuple[Harness, str]:
    """A call with a draft the model has just been handed, on a clock the test moves."""
    patient = LiveConfig(api_key="k", idle_seconds=10_000, max_call_seconds=100_000)
    h = await start(tmp_path, clock=clock, config=patient)
    await h.rig.publish(MINE, title="Fix flaky auth test", state="idle")
    h.upstream.push(function_call("prepare_message", {"session": "auth", "text": "Go on"}, "c1"))
    await until(lambda: bool(h.browser.of("draft")))
    return h, h.browser.of("draft")[0]["draft"]["id"]


async def confirm(h: Harness, draft_id: str, call_id: str) -> dict[str, Any]:
    """The model's confirm_send, and what the gateway answered it."""
    before = len(h.upstream.of("response.item.create"))
    h.upstream.push(function_call("confirm_send", {"draft_id": draft_id}, call_id))
    await until(lambda: len(h.upstream.of("response.item.create")) == before + 1)
    output = h.upstream.of("response.item.create")[-1]["item"]["output"]
    answer: dict[str, Any] = json.loads(output)
    return answer


#: What a read-back lasts in these tests: more than the gate asks for (``READBACK_SECONDS``).
READ_BACK_SECONDS = 2.2


async def read_back_sent(h: Harness, clock: FakeClock) -> None:
    """The assistant reads the draft back and all of it has gone to the browser.

    The lead goes at once and the rest as the clock moves on, so the clock stands 1 s into the
    read-back: it finishes playing ``READ_BACK_SECONDS - 1`` s from now.
    """
    before = h.browser.audio_ms()
    h.upstream.push(audio(pcm_ms(round(READ_BACK_SECONDS * 1000))))
    await until(lambda: h.browser.audio_ms() >= before + 1500)
    clock.advance(1.0)
    await until(lambda: h.browser.audio_ms() >= before + READ_BACK_SECONDS * 1000)


async def read_back(h: Harness, clock: FakeClock) -> None:
    """The assistant reads the draft back, and it finishes being heard."""
    await read_back_sent(h, clock)
    clock.advance(3)


async def person_says(h: Harness, transcript: str | None, *, frames: int = SPEECH_FRAMES) -> None:
    """The person speaks (the detector hears it), and the words are transcribed."""
    sent = len(h.upstream.of("session.input_audio.append"))
    h.browser.mic(QUIET)  # the microphone never stops: this is what ends an earlier utterance
    for _ in range(frames):
        h.browser.mic(LOUD)
    await until(lambda: len(h.upstream.of("session.input_audio.append")) == sent + 1 + frames)
    if transcript is not None:
        h.upstream.push({"type": "session.input_transcript.delta", "delta": transcript})
        await h.upstream.settled()


async def person_speaks(h: Harness, *, frames: int = SPEECH_FRAMES) -> None:
    """Loud microphone frames long enough for speech to begin, all at the clock's current time."""
    sent = len(h.upstream.of("session.input_audio.append"))
    h.browser.mic(QUIET)
    for _ in range(frames):
        h.browser.mic(LOUD)
    await until(lambda: len(h.upstream.of("session.input_audio.append")) == sent + 1 + frames)


async def person_stops(h: Harness) -> None:
    """One quiet frame, after the clock has moved on: the detector ends the speech with it."""
    sent = len(h.upstream.of("session.input_audio.append"))
    h.browser.mic(QUIET)
    await until(lambda: len(h.upstream.of("session.input_audio.append")) == sent + 1)


async def person_talks_on(h: Harness) -> None:
    """One more loud frame of the person's speech, at the clock's current time."""
    sent = len(h.upstream.of("session.input_audio.append"))
    h.browser.mic(LOUD)
    await until(lambda: len(h.upstream.of("session.input_audio.append")) == sent + 1)


async def model_stops(h: Harness) -> None:
    """The model stops talking, as it does for a person it hears: its stream goes on quiet, as
    much of it as says it stopped."""
    h.upstream.push(audio(noise_floor_ms(round(YIELD_SILENCE_SECONDS * 1000))))
    await h.upstream.settled()


async def model_talks_on(h: Harness, clock: FakeClock, seconds: float = 1.2) -> None:
    """The model goes on talking, at the pace the service streams it: 100 ms every 100 ms."""
    for _ in range(round(seconds * 10)):
        clock.advance(0.1)
        h.upstream.push(audio(pcm_ms(100)))
        await h.upstream.settled()


def barge_in_lines(caplog: pytest.LogCaptureFixture) -> list[dict[str, Any]]:
    """The line each speech start over the assistant left, once decided."""
    return [
        r.fields  # type: ignore[attr-defined]
        for r in caplog.records
        if r.getMessage() == "live barge-in"
    ]
