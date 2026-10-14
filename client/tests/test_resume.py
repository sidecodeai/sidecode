"""Amendment A35: reading a usage-limit stop, and resuming the session itself."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, ClassVar

import pytest

from rc_client.agents.base import Emit
from rc_client.agents.claude import transcripts
from rc_client.agents.codex.translate import CodexTranslator
from rc_client.errors import RcError
from rc_client.events import HISTORY_KINDS, dedup_key, should_store
from rc_client.models import AgentInfo, Session, SessionResume
from rc_client.registry import Registry
from rc_client.sessions.hub import SessionEntry, SessionHub
from rc_client.sessions.limits import (
    FIVE_HOURS,
    SEVEN_DAYS,
    LimitStop,
    TurnEnd,
    claude_result_limit,
    claude_row_limit,
    claude_transcript_limit,
    codex_limit_windows,
    codex_turn_limit,
)
from rc_client.sessions.resume import (
    ALREADY_RUNNING,
    OUT_OF_ATTEMPTS,
    PERSON_SENT,
    RESUME_PROMPT,
    SWITCHED_OFF,
    TERMINAL_CLOSED,
    ResumeScheduler,
)
from tests.helpers import event_validator, load_fixture
from tests.test_hub import FakeRunner
from tests.test_hub import agent_info as hub_agent_info


def agent_info() -> list[AgentInfo]:
    """The hub's fake agents, with the capability the command test needs (A27)."""
    agents = hub_agent_info()
    agents[0].capabilities = [*agents[0].capabilities, "commands"]
    return agents


# The row Claude Code writes when the five-hour window is used up, as it was
# recorded on a real machine (identifiers shortened).
LIMIT_ROW: dict[str, Any] = {
    "type": "assistant",
    "uuid": "row-limit",
    "parentUuid": "row-prompt",
    "sessionId": "sess-1",
    "timestamp": "2026-09-10T14:20:01.000Z",
    "isSidechain": False,
    "userType": "external",
    "cwd": "/repo",
    "version": "2.1.273",
    "gitBranch": "master",
    "message": {
        "id": "msg-limit",
        "model": "<synthetic>",
        "role": "assistant",
        "type": "message",
        "stop_reason": "stop_sequence",
        "stop_sequence": "",
        "usage": {"input_tokens": 0, "output_tokens": 0},
        "content": [
            {
                "type": "text",
                "text": "You've hit your session limit · resets 2:20pm (UTC)",
            }
        ],
    },
    "requestId": "req-1",
    "isApiErrorMessage": True,
    "apiErrorStatus": 429,
    "error": "rate_limit",
    "quotaLimits": {
        "rateLimitType": "five_hour",
        "resetsAt": 1788963600,
        "status": "rejected",
        "isUsingOverage": False,
        "overageStatus": "rejected",
        "unifiedRateLimitFallbackAvailable": False,
    },
}

NOW = 1788948000000
MINUTE = 60 * 1000
HOUR = 60 * MINUTE


class Clock:
    """A clock a test moves by hand, so no rule of 7.2 waits on real time."""

    def __init__(self, now: int = NOW) -> None:
        self.now = now

    def __call__(self) -> int:
        return self.now

    def advance(self, millis: int) -> None:
        self.now += millis


class Harness:
    """One hub, one session and a scheduler on a clock the test owns."""

    def __init__(self, tmp_path: Path, control: str = "remote") -> None:
        self.registry = Registry(tmp_path / "state.sqlite3")
        self.frames: list[dict[str, Any]] = []
        self.clock = Clock()
        self.hub = SessionHub(self.registry, self._publish, "dev-1", agent_info)
        self.hub.resumes = ResumeScheduler(self.hub, self.clock)
        self.entry = self._add_session(control)

    async def _publish(self, frame: dict[str, Any]) -> None:
        self.frames.append(frame)

    def _add_session(self, control: str) -> SessionEntry:
        session = Session(
            session_id="sess-1",
            device_id="dev-1",
            agent="claude",
            cwd="/repo",
            state="idle",
            control=control,  # type: ignore[arg-type]
        )
        self.registry.upsert_session(session)
        channel = self.hub._channel(session)
        entry = SessionEntry(session=session, channel=channel)
        entry.runner = FakeRunner(channel)
        self.hub.entries[session.session_id] = entry
        return entry

    @property
    def runner(self) -> FakeRunner:
        runner = self.entry.runner
        assert isinstance(runner, FakeRunner)
        return runner

    def events(self, kind: str) -> list[dict[str, Any]]:
        return [
            frame["event"]
            for frame in self.frames
            if frame.get("type") == "session.event" and frame["event"]["kind"] == kind
        ]

    async def hit_limit(self, limit: LimitStop) -> None:
        """End the session's turn the way a usage-limit stop does."""
        await self.entry.channel.begin_turn("remote")
        self.runner._busy = False
        await self.entry.channel.end_turn("error", 1200, limit=limit)

    def close(self) -> None:
        self.registry.close()


@pytest.fixture
def harness(tmp_path: Path) -> Any:
    built = Harness(tmp_path)
    yield built
    built.close()


# ------------------------------------------------------------------ fixtures


def test_the_resume_fixtures_decode_as_this_device_publishes_them() -> None:
    """Every A35 fixture, through the device's own types."""
    pending = load_fixture("objects/session.resume-pending.json")
    session = Session.from_dict(pending)
    assert session.resume == SessionResume(
        at=1788966060000, estimated=False, attempts=0, window_minutes=FIVE_HOURS
    )
    assert session.to_dict()["resume"] == pending["resume"]

    completed = load_fixture("events/turn_completed.limit.json")
    assert completed["stop_reason"] == "error"
    stop = LimitStop(window_minutes=FIVE_HOURS, resets_at_ms=1788966000000)
    assert stop.to_dict() == completed["limit"]

    assert load_fixture("events/turn_started.resume.json")["trigger"] == "resume"
    message = load_fixture("events/user_message.resume.json")
    assert message["source"] == "resume"
    assert message["text"] == RESUME_PROMPT

    frame = load_fixture("device/preferences.json")
    assert frame["type"] == "preferences"
    assert frame["preferences"]["resume_after_limit"] is True


def test_a_resume_row_is_kept_for_the_timeline() -> None:
    """Like `notice`, a resume is a row a reload and a backfill must replay."""
    for name in ("events/resume.json", "events/resume.dropped.json"):
        event = load_fixture(name)
        assert event["kind"] in HISTORY_KINDS
        assert dedup_key(event) is not None
        assert should_store(event) is True
    assert load_fixture("events/resume.dropped.json")["reason"] == TERMINAL_CLOSED


def test_a_limit_stop_with_no_time_still_says_there_was_one() -> None:
    assert LimitStop().to_dict() == {"resets_at": None}
    assert LimitStop(window_minutes=SEVEN_DAYS).to_dict() == {
        "window_minutes": SEVEN_DAYS,
        "resets_at": None,
    }


# --------------------------------------------------------------- reading it


def test_the_claude_transcript_row_is_read_as_a_limit_and_not_as_words() -> None:
    """A35: the CLI's 429 row ends the turn as an error and speaks as one."""
    tailer = transcripts.TranscriptTailer(path="/nonexistent", cwd="/repo")
    tailer.translate(
        {"type": "user", "uuid": "row-prompt", "message": {"role": "user", "content": "go"}}
    )
    running = tailer.busy

    emits = tailer.translate(LIMIT_ROW)
    assert [emit.kind for emit in emits] == ["error"]
    assert "session limit" in emits[0].fields["message"]
    assert running and not tailer.busy
    assert tailer.stop_reason == "error"
    assert tailer.limit == LimitStop(window_minutes=FIVE_HOURS, resets_at_ms=1788963600000)


def test_a_new_turn_forgets_the_limit_the_last_one_ended_at() -> None:
    tailer = transcripts.TranscriptTailer(path="/nonexistent", cwd="/repo")
    tailer.translate(LIMIT_ROW)
    first = tailer.limit
    tailer.translate(
        {"type": "user", "uuid": "row-next", "message": {"role": "user", "content": "again"}}
    )
    assert first == LimitStop(FIVE_HOURS, 1788963600000)
    assert tailer.limit is None
    assert tailer.stop_reason == "completed"


def test_a_row_the_vendor_refused_for_another_reason_is_not_a_limit() -> None:
    other = dict(LIMIT_ROW, apiErrorStatus=500, error="overloaded")
    assert claude_row_limit(other) is None
    assert claude_row_limit({"type": "assistant", "message": {"content": []}}) is None


def test_a_limit_row_without_a_window_the_device_knows_carries_no_window() -> None:
    weekly = dict(LIMIT_ROW, quotaLimits={"rateLimitType": "seven_day", "resetsAt": 1788963600})
    assert claude_row_limit(weekly) == LimitStop(SEVEN_DAYS, 1788963600000)
    unknown = dict(LIMIT_ROW, quotaLimits={"rateLimitType": "monthly"})
    assert claude_row_limit(unknown) == LimitStop(None, None)


def test_the_sdk_result_says_there_is_a_limit_and_the_transcript_says_when(
    tmp_path: Path,
) -> None:
    """A35: `api_error_status` 429 is the whole of the SDK's account of it."""
    assert claude_result_limit(429) == LimitStop()
    assert claude_result_limit(500) is None
    assert claude_result_limit(None) is None

    path = tmp_path / "sess-1.jsonl"
    rows = [
        {"type": "user", "uuid": "row-prompt", "message": {"role": "user", "content": "go"}},
        LIMIT_ROW,
    ]
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    assert claude_transcript_limit(path) == LimitStop(FIVE_HOURS, 1788963600000)
    assert claude_transcript_limit(tmp_path / "absent.jsonl") is None


def test_the_sdk_turn_ends_as_an_error_carrying_the_limit() -> None:
    from rc_client.agents.claude.translate import ClaudeTranslator

    class Result:
        subtype = "error_during_execution"
        is_error = True
        result = "usage limit reached"
        duration_ms = 1200
        usage: ClassVar[dict[str, Any]] = {}
        total_cost_usd = None
        session_id = "sess-1"
        api_error_status = 429

    emits = ClaudeTranslator(cwd="/repo")._feed_result(Result())  # type: ignore[arg-type]
    completed = next(emit for emit in emits if emit.kind == "turn_completed")
    assert completed.fields["stop_reason"] == "error"
    assert completed.fields["limit"] == LimitStop()


def test_the_codex_turn_names_the_usage_limit_and_the_quota_read_names_the_window() -> None:
    """A35: `usageLimitExceeded`, plus the window the account has used most of."""
    turn = {
        "id": "turn-1",
        "status": "failed",
        "durationMs": 1200,
        "error": {
            "message": "You've hit your usage limit.",
            "codexErrorInfo": "usageLimitExceeded",
        },
    }
    assert codex_turn_limit(turn) == LimitStop()
    assert codex_turn_limit(dict(turn, status="completed")) is None
    assert codex_turn_limit({"status": "failed", "error": {"codexErrorInfo": "badRequest"}}) is None

    reply = {
        "rateLimits": {
            "primary": {"windowDurationMins": 300, "usedPercent": 100, "resetsAt": 1788966000},
            "secondary": {"windowDurationMins": 10080, "usedPercent": 42, "resetsAt": 1789400000},
        }
    }
    assert codex_limit_windows(reply) == LimitStop(FIVE_HOURS, 1788966000000)
    assert codex_limit_windows(None) == LimitStop()


def test_a_codex_turn_that_hit_the_limit_ends_with_one() -> None:
    translator = CodexTranslator(cwd="/repo")
    translator.turn_id = "turn-1"
    emits = translator.notification(
        "turn/completed",
        {
            "threadId": "thread-1",
            "turn": {
                "id": "turn-1",
                "status": "failed",
                "durationMs": 1200,
                "error": {"message": "limit", "codexErrorInfo": "usageLimitExceeded"},
            },
        },
    )
    completed = next(emit for emit in emits if emit.kind == "turn_completed")
    assert completed.fields["stop_reason"] == "error"
    assert completed.fields["limit"] == LimitStop()


@pytest.mark.parametrize("agent", ["grok", "pi"])
def test_grok_and_pi_turns_never_carry_a_limit(agent: str) -> None:
    """7.2: neither reports a limit the device can read, so no turn carries one."""
    if agent == "grok":
        from rc_client.agents.grok.translate import GrokTranslator

        emits: list[Emit] = GrokTranslator(cwd="/repo")._on_turn_completed(
            {"stop_reason": "error", "elapsed_ms": 10}, {}
        )
    else:
        from rc_client.agents.pi.translate import PiTranslator

        translator = PiTranslator()
        translator.stop_reason = "error"
        emits = translator.event({"type": "agent_settled"})
    completed = [emit for emit in emits if emit.kind == "turn_completed"]
    assert completed and all("limit" not in emit.fields for emit in completed)


# ---------------------------------------------------------------- scheduling


async def enable(harness: Harness) -> None:
    await harness.hub.resumes.set_enabled(True)


async def test_a_limit_stop_schedules_a_resume_a_minute_after_the_reset(
    harness: Harness,
) -> None:
    await enable(harness)
    await harness.hit_limit(LimitStop(FIVE_HOURS, NOW + HOUR))

    scheduled = harness.events("resume")[-1]
    assert scheduled["status"] == "scheduled"
    assert scheduled["at"] == NOW + HOUR + MINUTE
    assert scheduled["estimated"] is False
    assert harness.entry.session.resume == SessionResume(
        at=NOW + HOUR + MINUTE, estimated=False, attempts=0, window_minutes=FIVE_HOURS
    )
    completed = harness.events("turn_completed")[-1]
    assert completed["limit"] == {"window_minutes": FIVE_HOURS, "resets_at": NOW + HOUR}


async def test_a_limit_with_no_reset_time_is_estimated_from_the_window(
    harness: Harness,
) -> None:
    await enable(harness)
    await harness.hit_limit(LimitStop(window_minutes=SEVEN_DAYS))
    scheduled = harness.events("resume")[-1]
    assert scheduled["estimated"] is True
    assert scheduled["at"] == NOW + SEVEN_DAYS * MINUTE


async def test_a_limit_with_neither_falls_back_to_five_hours(harness: Harness) -> None:
    await enable(harness)
    await harness.hit_limit(LimitStop())
    assert harness.events("resume")[-1]["at"] == NOW + FIVE_HOURS * MINUTE


async def test_nothing_is_scheduled_while_the_switch_is_off(harness: Harness) -> None:
    await harness.hit_limit(LimitStop(FIVE_HOURS, NOW + HOUR))
    assert harness.events("resume") == []
    assert harness.entry.session.resume is None


async def test_a_terminal_session_gets_no_resume(tmp_path: Path) -> None:
    """7.2: the device has no way into a session the terminal controls."""
    built = Harness(tmp_path, control="terminal")
    await enable(built)
    await built.hit_limit(LimitStop(FIVE_HOURS, NOW + HOUR))
    assert built.events("resume") == []
    built.close()


async def test_the_resume_sends_the_fixed_prompt_as_the_person(harness: Harness) -> None:
    """7.2: `user_message {source: "resume"}` under `trigger: "resume"`."""
    await enable(harness)
    await harness.hit_limit(LimitStop(FIVE_HOURS, NOW + HOUR))
    harness.clock.advance(HOUR + 2 * MINUTE)
    await harness.hub.resumes.tick()

    assert [event["status"] for event in harness.events("resume")] == ["scheduled", "fired"]
    assert harness.runner.sent == [RESUME_PROMPT]
    assert harness.runner.sources == ["resume"]
    assert harness.events("turn_started")[-1]["trigger"] == "resume"
    assert harness.entry.session.resume is None


async def test_a_resume_whose_time_has_not_come_stays_pending(harness: Harness) -> None:
    await enable(harness)
    await harness.hit_limit(LimitStop(FIVE_HOURS, NOW + HOUR))
    await harness.hub.resumes.tick()
    assert harness.runner.sent == []
    assert harness.entry.session.resume is not None


async def test_a_session_someone_took_further_cancels_its_resume(harness: Harness) -> None:
    await enable(harness)
    await harness.hit_limit(LimitStop(FIVE_HOURS, NOW + HOUR))
    harness.entry.session.state = "running"
    harness.clock.advance(HOUR + 2 * MINUTE)
    await harness.hub.resumes.tick()

    cancelled = harness.events("resume")[-1]
    assert cancelled["status"] == "cancelled"
    assert cancelled["reason"] == ALREADY_RUNNING
    assert harness.runner.sent == []
    assert harness.entry.session.resume is None


async def test_a_terminal_closed_before_the_time_drops_the_resume(tmp_path: Path) -> None:
    """7.2: the person who closed the terminal has said they are done."""
    built = Harness(tmp_path, control="shared")
    await enable(built)
    await built.hit_limit(LimitStop(FIVE_HOURS, NOW + HOUR))
    built.entry.session.control = "none"
    built.clock.advance(HOUR + 2 * MINUTE)
    await built.hub.resumes.tick()

    dropped = built.events("resume")[-1]
    assert dropped["status"] == "dropped"
    assert dropped["reason"] == TERMINAL_CLOSED
    assert built.runner.sent == []
    built.close()


async def test_a_session_the_device_runs_is_resumed_even_with_no_process(
    harness: Harness,
) -> None:
    """A `none` session the device runs is resumed first, as 6.3 says."""
    await enable(harness)
    await harness.hit_limit(LimitStop(FIVE_HOURS, NOW + HOUR))
    harness.entry.session.control = "none"
    harness.clock.advance(HOUR + 2 * MINUTE)
    await harness.hub.resumes.tick()
    assert harness.runner.sent == [RESUME_PROMPT]


async def test_a_message_the_person_sends_cancels_the_resume(harness: Harness) -> None:
    await enable(harness)
    await harness.hit_limit(LimitStop(FIVE_HOURS, NOW + HOUR))
    await harness.hub.send({"id": "req-1", "session_id": "sess-1", "text": "go", "mode": "auto"})

    cancelled = harness.events("resume")[-1]
    assert cancelled["status"] == "cancelled"
    assert cancelled["reason"] == PERSON_SENT
    assert harness.entry.session.resume is None


async def test_a_command_the_person_runs_cancels_the_resume(harness: Harness) -> None:
    await enable(harness)
    await harness.hit_limit(LimitStop(FIVE_HOURS, NOW + HOUR))
    await harness.hub.command({"id": "req-1", "session_id": "sess-1", "name": "compact"})
    assert harness.events("resume")[-1]["status"] == "cancelled"


async def test_turning_the_switch_off_cancels_every_pending_resume(harness: Harness) -> None:
    await enable(harness)
    await harness.hit_limit(LimitStop(FIVE_HOURS, NOW + HOUR))
    await harness.hub.resumes.set_enabled(False)

    cancelled = harness.events("resume")[-1]
    assert cancelled["status"] == "cancelled"
    assert cancelled["reason"] == SWITCHED_OFF
    assert harness.entry.session.resume is None


async def test_turning_the_switch_on_schedules_nothing_for_stops_already_past(
    harness: Harness,
) -> None:
    await harness.hit_limit(LimitStop(FIVE_HOURS, NOW + HOUR))
    await enable(harness)
    assert harness.events("resume") == []


async def test_a_resumed_turn_that_hits_the_limit_again_is_rescheduled(
    harness: Harness,
) -> None:
    """7.2: `rescheduled` with `attempts + 1`, and dropped after the third."""
    await enable(harness)
    statuses: list[str] = []
    for attempt in range(4):
        await harness.hit_limit(LimitStop(FIVE_HOURS, harness.clock.now + HOUR))
        statuses.append(harness.events("resume")[-1]["status"])
        harness.clock.advance(HOUR + 2 * MINUTE)
        await harness.hub.resumes.tick()
        if attempt < 3:
            assert harness.events("resume")[-1]["status"] == "fired"

    assert statuses == ["scheduled", "rescheduled", "rescheduled", "dropped"]
    assert harness.events("resume")[-1]["reason"] == OUT_OF_ATTEMPTS
    assert [event.get("attempts") for event in harness.events("resume") if "attempts" in event] == [
        1,
        2,
    ]
    assert harness.entry.session.resume is None


async def test_a_turn_that_got_past_the_limit_starts_the_attempts_again(
    harness: Harness,
) -> None:
    await enable(harness)
    await harness.hit_limit(LimitStop(FIVE_HOURS, NOW + HOUR))
    harness.clock.advance(HOUR + 2 * MINUTE)
    await harness.hub.resumes.tick()
    await harness.entry.channel.begin_turn("resume")
    await harness.entry.channel.end_turn("completed", 10)

    await harness.hit_limit(LimitStop(FIVE_HOURS, harness.clock.now + HOUR))
    assert harness.events("resume")[-1]["status"] == "scheduled"


async def test_a_pending_resume_survives_a_restart(tmp_path: Path) -> None:
    """7.2: the record is kept across the device's own restarts."""
    built = Harness(tmp_path)
    await enable(built)
    await built.hit_limit(LimitStop(FIVE_HOURS, NOW + HOUR))
    at = built.entry.session.resume.at if built.entry.session.resume else 0
    built.registry.flush()
    built.close()

    restarted = Harness(tmp_path)
    restarted.hub.entries.clear()
    restarted.hub.load()
    restarted.hub.resumes.load()
    assert restarted.hub.resumes.enabled is True
    session = restarted.hub.entries["sess-1"].session
    assert session.resume is not None and session.resume.at == at
    restarted.close()


async def test_a_resume_for_a_session_that_is_gone_is_forgotten(tmp_path: Path) -> None:
    built = Harness(tmp_path)
    await enable(built)
    await built.hit_limit(LimitStop(FIVE_HOURS, NOW + HOUR))
    built.registry.flush()
    built.close()

    restarted = Harness(tmp_path)
    restarted.hub.entries.clear()
    restarted.registry.delete_session("sess-1")
    restarted.hub.resumes.load()
    assert restarted.registry.load_resumes() == []
    restarted.close()


# ------------------------------------------------------------ the two requests


async def test_a_person_can_set_and_move_and_cancel_a_resume(harness: Harness) -> None:
    at = NOW + 2 * HOUR
    result = await harness.hub.resumes.set_request({"session_id": "sess-1", "at": at})
    assert result["session"]["resume"] == {"at": at, "estimated": False, "attempts": 0}
    assert harness.events("resume")[-1] == {
        **harness.events("resume")[-1],
        "status": "scheduled",
        "at": at,
        "estimated": False,
    }

    moved = await harness.hub.resumes.set_request({"session_id": "sess-1", "at": at + HOUR})
    assert moved["session"]["resume"]["at"] == at + HOUR
    assert harness.events("resume")[-1]["status"] == "rescheduled"

    cancelled = await harness.hub.resumes.cancel_request({"session_id": "sess-1"})
    assert cancelled["session"]["resume"] is None
    assert harness.events("resume")[-1]["status"] == "cancelled"
    # Idempotent: a second cancel says nothing and still answers.
    rows = len(harness.events("resume"))
    again = await harness.hub.resumes.cancel_request({"session_id": "sess-1"})
    assert again["session"]["resume"] is None
    assert len(harness.events("resume")) == rows


@pytest.mark.parametrize(
    "at",
    [NOW + 30 * 1000, NOW - HOUR, NOW + 9 * 24 * 60 * MINUTE, "soon", None],
)
async def test_a_resume_time_outside_the_window_is_refused(harness: Harness, at: Any) -> None:
    """6.3: at least a minute ahead, and within eight days."""
    with pytest.raises(RcError) as raised:
        await harness.hub.resumes.set_request({"session_id": "sess-1", "at": at})
    assert raised.value.code == "bad_request"


async def test_a_resume_cannot_be_set_while_a_turn_runs(harness: Harness) -> None:
    harness.entry.session.state = "running"
    with pytest.raises(RcError) as raised:
        await harness.hub.resumes.set_request({"session_id": "sess-1", "at": NOW + 2 * HOUR})
    assert raised.value.code == "conflict"


async def test_a_resume_cannot_be_set_on_a_terminal_session(tmp_path: Path) -> None:
    built = Harness(tmp_path, control="terminal")
    with pytest.raises(RcError) as raised:
        await built.hub.resumes.set_request({"session_id": "sess-1", "at": NOW + 2 * HOUR})
    assert raised.value.code == "conflict"
    built.close()


async def test_the_preferences_frame_is_what_turns_the_switch_on(tmp_path: Path) -> None:
    """7: the gateway tells the device after `hello_ack` and on every change."""
    from rc_client.config import Config
    from rc_client.daemon import Daemon

    daemon = Daemon(
        Config(
            gateway_origin="https://rc.example.com",
            device_id="dev-1",
            device_token="t",
            name="test",
        )
    )
    try:
        seen = [daemon.hub.resumes.enabled]
        await daemon._preferences(load_fixture("device/preferences.json"))
        seen.append(daemon.hub.resumes.enabled)
        await daemon._preferences({"type": "preferences", "preferences": {}})
        seen.append(daemon.hub.resumes.enabled)
        assert seen == [False, True, False]
    finally:
        daemon.registry.close()


async def test_a_shared_session_ends_at_the_limit_and_is_resumed_by_injection(
    tmp_path: Path,
) -> None:
    """The whole path for a terminal session: transcript row, schedule, injection."""
    from tests.test_shared_control import Harness as SharedHarness

    built = SharedHarness(tmp_path)
    clock = Clock()
    built.hub.resumes = ResumeScheduler(built.hub, clock)
    await built.hub.resumes.set_enabled(True)
    entry = await built.attach()

    tailer = transcripts.TranscriptTailer(path="/nonexistent", cwd="/repo")
    tailer.translate(
        {"type": "user", "uuid": "row-prompt", "message": {"role": "user", "content": "go"}}
    )
    await built.hub.shared.tick(entry, tailer.busy, tailer.turn_trigger, TurnEnd())
    for emit in tailer.translate(LIMIT_ROW):
        await entry.channel.emit(emit.kind, **emit.fields)
    await built.hub.shared.tick(
        entry, tailer.busy, tailer.turn_trigger, TurnEnd(tailer.stop_reason, tailer.limit)
    )

    completed = built.events("turn_completed")[-1]
    assert completed["stop_reason"] == "error"
    assert completed["limit"] == {"window_minutes": FIVE_HOURS, "resets_at": 1788963600000}
    assert built.events("assistant_text") == []
    assert "session limit" in built.events("error")[-1]["message"]
    scheduled = built.events("resume")[-1]
    assert scheduled["status"] == "scheduled"
    assert scheduled["at"] == 1788963600000 + MINUTE

    clock.now = scheduled["at"]
    await built.hub.resumes.tick()
    assert [text for _, text in built.attachment.injected] == [RESUME_PROMPT]
    assert built.events("user_message")[-1]["source"] == "resume"
    assert built.events("turn_started")[-1]["trigger"] == "resume"

    validator = event_validator()
    if validator is not None:
        for frame in built.frames:
            if frame.get("type") == "session.event":
                validator.validate(frame["event"])
    built.registry.close()


async def test_every_event_a_resume_publishes_validates_against_the_schema(
    harness: Harness,
) -> None:
    """A35: the limit stop, the schedule, the fire and the prompt, all on the wire."""
    validator = event_validator()
    if validator is None:
        pytest.skip("the protocol schema is not present")
    await harness.hub.resumes.set_enabled(True)
    await harness.hit_limit(LimitStop(FIVE_HOURS, NOW + HOUR))
    harness.clock.advance(HOUR + 2 * MINUTE)
    await harness.hub.resumes.tick()
    await harness.runner.finish()
    await harness.hub.resumes.set_request({"session_id": "sess-1", "at": harness.clock.now + HOUR})
    await harness.hub.resumes.cancel_request({"session_id": "sess-1"})

    published = [frame["event"] for frame in harness.frames if frame.get("type") == "session.event"]
    assert {"turn_completed", "resume", "turn_started"} <= {event["kind"] for event in published}
    for event in published:
        validator.validate(event)
