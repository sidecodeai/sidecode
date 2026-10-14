"""Terminal-session mirroring: transcript and rollout tailing plus holder logic."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import pytest

from rc_client.agents.claude import holders as claude_holders
from rc_client.agents.claude import transcripts as claude_transcripts
from rc_client.agents.claude.holders import (
    Holder,
    HolderScan,
    SessionRef,
    _is_attachable,
    _looks_like_claude,
    _session_from_argv,
)
from rc_client.agents.claude.transcripts import (
    SESSION_SETTINGS,
    TranscriptInfo,
    TranscriptTailer,
    latest_settings,
)
from rc_client.agents.codex import rollouts as codex_rollouts
from rc_client.agents.codex.rollouts import RolloutTailer
from rc_client.models import AgentInfo, Session
from rc_client.procscan import Proc
from rc_client.registry import Registry
from rc_client.sessions import mirror as mirror_module
from rc_client.sessions.attach import Attachment
from rc_client.sessions.hub import SessionHub
from rc_client.sessions.mirror import MirrorService


def write_rows(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")


def user_row(text: str, uuid: str = "u1") -> dict[str, Any]:
    return {"type": "user", "uuid": uuid, "message": {"role": "user", "content": text}}


def assistant_row(
    content: list[dict[str, Any]], message_id: str = "msg_1", stop_reason: str | None = None
) -> dict[str, Any]:
    message: dict[str, Any] = {"id": message_id, "content": content}
    if stop_reason is not None:
        message["stop_reason"] = stop_reason
    return {"type": "assistant", "uuid": "a1", "message": message}


def model_row(model_id: str) -> dict[str, Any]:
    identity = {"modelId": model_id, "marketingName": "Fable 5.1"}
    return {"type": "attachment", "attachment": {"type": "model", "identity": identity}}


def permission_row(mode: str) -> dict[str, Any]:
    return {"type": "permission-mode", "permissionMode": mode, "sessionId": "live"}


def effort_row(effort: str, text: str = "ok") -> dict[str, Any]:
    return dict(assistant_row([{"type": "text", "text": text}]), effort=effort)


def test_a_sentence_before_the_tool_calls_does_not_end_the_turn() -> None:
    """A34: the CLI files each block of a message as its own row, all carrying
    the message's final `stop_reason`, so a text-only row of a turn that goes on
    to call tools looks exactly like the last row of a turn. Only the CLI's own
    word ends it."""
    tailer = TranscriptTailer(path="/nonexistent", cwd="/repo")
    tailer.awaiting_reply = True

    emits = tailer.translate(
        assistant_row([{"type": "text", "text": "Let me read the file."}], stop_reason="tool_use")
    )

    assert [emit.kind for emit in emits] == ["assistant_text"]
    assert tailer.busy is True


def test_the_tool_call_row_of_the_same_message_leaves_the_turn_running() -> None:
    tailer = TranscriptTailer(path="/nonexistent", cwd="/repo")
    tailer.awaiting_reply = True

    tailer.translate(
        assistant_row(
            [{"type": "tool_use", "id": "t1", "name": "Read", "input": {"file_path": "/repo/a"}}],
            stop_reason="tool_use",
        )
    )

    assert tailer.busy is True


@pytest.mark.parametrize("stop_reason", ["end_turn", "stop_sequence", "max_tokens", None])
def test_every_stop_reason_but_tool_use_ends_the_turn(stop_reason: str | None) -> None:
    tailer = TranscriptTailer(path="/nonexistent", cwd="/repo")
    tailer.awaiting_reply = True

    tailer.translate(assistant_row([{"type": "text", "text": "done"}], stop_reason=stop_reason))

    assert tailer.busy is False


def test_tailer_reads_only_appended_bytes(tmp_path: Path) -> None:
    path = tmp_path / "t.jsonl"
    write_rows(path, [user_row("first")])
    tailer = TranscriptTailer(path=str(path), cwd="/repo")
    assert len(tailer.read_new()) == 1
    assert tailer.read_new() == []
    write_rows(path, [user_row("second", uuid="u2")])
    rows = tailer.read_new()
    assert len(rows) == 1
    assert rows[0]["uuid"] == "u2"


def test_tailer_ignores_a_partial_trailing_line(tmp_path: Path) -> None:
    path = tmp_path / "t.jsonl"
    write_rows(path, [user_row("first")])
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"type": "user", "uuid"')
    tailer = TranscriptTailer(path=str(path), cwd="/repo")
    assert len(tailer.read_new()) == 1
    assert tailer.read_new() == []


def test_tailer_restarts_when_the_file_shrinks(tmp_path: Path) -> None:
    path = tmp_path / "t.jsonl"
    write_rows(path, [user_row("first"), user_row("second", uuid="u2")])
    tailer = TranscriptTailer(path=str(path), cwd="/repo")
    tailer.read_new()
    path.write_text("")
    assert tailer.read_new() == []
    write_rows(path, [user_row("fresh", uuid="u9")])
    assert [row["uuid"] for row in tailer.read_new()] == ["u9"]


def test_tailer_restarts_when_the_file_is_replaced(tmp_path: Path) -> None:
    path = tmp_path / "t.jsonl"
    write_rows(path, [user_row("first")])
    tailer = TranscriptTailer(path=str(path), cwd="/repo")
    tailer.read_new()
    replacement = tmp_path / "new.jsonl"
    write_rows(replacement, [user_row("rotated", uuid="u9")])
    replacement.replace(path)
    assert [row["uuid"] for row in tailer.read_new()] == ["u9"]


def test_transcript_user_rows_become_terminal_user_messages() -> None:
    tailer = TranscriptTailer(path="/dev/null", cwd="/repo")
    emits = tailer.translate(user_row("please fix the test"))
    assert emits[0].kind == "user_message"
    assert emits[0].fields["source"] == "terminal"
    assert emits[0].fields["block_id"] == "u1"
    assert tailer.awaiting_reply is True


def test_transcript_meta_and_placeholder_rows_are_skipped() -> None:
    """A row the CLI marks as its own bookkeeping stays hidden whatever it holds.

    A command row it does not mark is the person's keystroke and does become a
    bubble (amendment A32, `test_claude_markers.py`), so the flag is what
    decides here, not the tag.
    """
    tailer = TranscriptTailer(path="/dev/null", cwd="/repo")
    meta = dict(user_row("<command-name>/clear</command-name>"), isMeta=True)
    assert tailer.translate(meta) == []
    answered = tailer.translate(user_row("<local-command-stdout>done"))
    # A40: reported to the device, never published to an app.
    assert [emit.kind for emit in answered] == [claude_transcripts.COMMAND_OUTPUT]
    assert tailer.translate(user_row("No response requested.")) == []
    assert tailer.awaiting_reply is False
    typed = user_row("<command-name>/clear</command-name>", uuid="u-clear")
    assert [emit.fields["text"] for emit in tailer.translate(typed)] == ["/clear"]


def test_transcript_assistant_text_clears_the_running_flag() -> None:
    tailer = TranscriptTailer(path="/dev/null", cwd="/repo")
    tailer.translate(user_row("go"))
    emits = tailer.translate(assistant_row([{"type": "text", "text": "all done"}]))
    assert emits[0].kind == "assistant_text"
    assert emits[0].fields == {"block_id": "msg_1:0", "text": "all done", "done": True}
    assert tailer.awaiting_reply is False


def test_transcript_tool_use_keeps_running_until_the_final_text() -> None:
    tailer = TranscriptTailer(path="/dev/null", cwd="/repo")
    tailer.translate(user_row("go"))
    call = tailer.translate(
        assistant_row(
            [{"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {"command": "ls"}}]
        )
    )
    assert call[0].fields["tool_kind"] == "shell"
    assert tailer.awaiting_reply is True
    result = tailer.translate(
        {
            "type": "user",
            "uuid": "u2",
            "message": {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "a\nb"}],
            },
            "toolUseResult": {"stdout": "a\nb"},
        }
    )
    assert result[0].fields["status"] == "succeeded"
    assert result[0].fields["output"] == "a\nb"


def test_transcript_rows_report_what_the_terminal_chose() -> None:
    tailer = TranscriptTailer(path="/dev/null", cwd="/repo")
    model = tailer.translate(model_row("claude-opus-5[1m]"))
    assert [(emit.kind, emit.fields) for emit in model] == [
        (SESSION_SETTINGS, {"model": "claude-opus-5[1m]"})
    ]
    mode = tailer.translate(permission_row("auto"))
    assert [(emit.kind, emit.fields) for emit in mode] == [
        (SESSION_SETTINGS, {"permission_mode": "auto"})
    ]
    # The effort rides on an assistant message, which still becomes its own text.
    effort = tailer.translate(effort_row("max", text="all done"))
    assert [emit.kind for emit in effort] == [SESSION_SETTINGS, "assistant_text"]
    assert effort[0].fields == {"effort": "max"}


def test_rows_of_an_unknown_settings_shape_report_nothing() -> None:
    tailer = TranscriptTailer(path="/dev/null", cwd="/repo")
    # The input mode, which is not the permission mode.
    assert tailer.translate({"type": "mode", "mode": "normal"}) == []
    assert tailer.translate({"type": "attachment", "attachment": {"type": "model"}}) == []
    assert tailer.translate(model_row("")) == []
    bad_identity = {"type": "attachment", "attachment": {"type": "model", "identity": 7}}
    assert tailer.translate(bad_identity) == []
    assert tailer.translate({"type": "permission-mode", "permissionMode": 7}) == []
    assert tailer.translate(dict(assistant_row([]), effort=None)) == []
    assert tailer.translate(dict(assistant_row([]), perTurnEffort="high")) == []


def test_latest_settings_returns_the_last_value_of_each(tmp_path: Path) -> None:
    path = tmp_path / "t.jsonl"
    write_rows(
        path,
        [
            model_row("claude-sonnet-4-5"),
            permission_row("default"),
            effort_row("high"),
            user_row("carry on", uuid="u2"),
            permission_row("bypassPermissions"),
            model_row("claude-fable-5-1"),
            effort_row("max"),
        ],
    )
    assert latest_settings(path) == {
        "model": "claude-fable-5-1",
        "permission_mode": "bypassPermissions",
        "effort": "max",
    }
    assert latest_settings(tmp_path / "absent.jsonl") == {}


def test_latest_settings_stops_at_its_cap(tmp_path: Path) -> None:
    path = tmp_path / "t.jsonl"
    write_rows(path, [model_row("claude-sonnet-4-5")])
    cap = path.stat().st_size
    write_rows(path, [model_row("claude-fable-5-1")])
    assert latest_settings(path, limit=cap) == {"model": "claude-sonnet-4-5"}
    assert latest_settings(path) == {"model": "claude-fable-5-1"}


def write_rollout(root: Path, thread_id: str, originator: str, source: Any) -> Path:
    """One rollout on disk, opened by the `session_meta` record Codex writes first."""
    directory = root / "2026" / "09" / "12"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"rollout-2026-09-12T10-00-00-{thread_id}.jsonl"
    meta = {
        "type": "session_meta",
        "payload": {
            "id": thread_id,
            "session_id": thread_id,
            "cwd": "/repo",
            "originator": originator,
            "source": source,
        },
    }
    write_rows(path, [meta])
    return path


def test_a_rollout_another_application_owns_is_never_mirrored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Amendments A18 and A48: provenance decides before anyone asks who holds the file."""
    root = tmp_path / "sessions"
    write_rollout(root, "t-tui", "codex-tui", "cli")
    write_rollout(root, "t-app-server-tui", "codex-tui", "vscode")
    write_rollout(root, "t-ours", "rc-client", "vscode")
    write_rollout(root, "t-desktop", "Codex Desktop", "vscode")
    write_rollout(root, "t-ide", "vscode-extension", "vscode")
    write_rollout(root, "t-sub", "codex-tui", {"subagent": {"other": "guardian"}})
    monkeypatch.setattr(codex_rollouts, "SESSIONS_DIR", root)

    found = {info.thread_id for info in codex_rollouts.discover()}
    assert found == {"t-tui", "t-app-server-tui", "t-ours"}


def test_the_rollout_limit_counts_what_this_device_may_mirror(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A machine whose history is mostly another application's still yields ours."""
    root = tmp_path / "sessions"
    now = time.time()
    for index in range(6):
        path = write_rollout(root, f"t-desktop-{index}", "Codex Desktop", "vscode")
        os.utime(path, (now, now - index))
    # The only thread of ours is the oldest, so a window of two files would hold
    # nothing but the desktop app's.
    os.utime(write_rollout(root, "t-tui", "codex-tui", "cli"), (now, now - 60))
    monkeypatch.setattr(codex_rollouts, "SESSIONS_DIR", root)

    assert [info.thread_id for info in codex_rollouts.discover(limit=2)] == ["t-tui"]


def test_rollout_tailer_tracks_turn_boundaries(tmp_path: Path) -> None:
    path = tmp_path / "rollout.jsonl"
    tailer = RolloutTailer(path=str(path), cwd="/repo")
    assert tailer.translate({"type": "event_msg", "payload": {"type": "task_started"}}) == []
    running_after_start = tailer.running
    emits = tailer.translate(
        {
            "type": "event_msg",
            "payload": {
                "type": "item_completed",
                "item": {
                    "type": "AgentMessage",
                    "id": "m1",
                    "content": [{"type": "Text", "text": "ok"}],
                },
            },
        }
    )
    assert emits[0].kind == "assistant_text"
    tailer.translate({"type": "event_msg", "payload": {"type": "task_complete"}})
    running_after_complete = tailer.running
    assert (running_after_start, running_after_complete) == (True, False)
    assert tailer.translate({"type": "response_item", "payload": {"type": "message"}}) == []


def test_claude_process_detection_and_session_extraction() -> None:
    live = Proc(pid=10, ppid=1, start="s", command="/Users/me/.local/bin/claude --resume abc-123")
    daemon = Proc(pid=11, ppid=1, start="s", command="/Users/me/.local/bin/claude daemon")
    other = Proc(pid=12, ppid=1, start="s", command="/usr/bin/python -m http.server")
    assert _looks_like_claude(live)
    assert not _looks_like_claude(daemon)
    assert not _looks_like_claude(other)
    assert _session_from_argv(live.argv) == "abc-123"
    assert _session_from_argv(["claude", "--session-id=xyz"]) == "xyz"
    assert _session_from_argv(["claude"]) is None
    assert _session_from_argv(["claude", "--resume"]) is None


def test_a_shim_started_claude_is_recognised_as_one_that_names_itself() -> None:
    plain = ["claude"]
    shimmed = ["claude", "--dangerously-load-development-channels", "server:rc"]
    joined = ["claude", "--dangerously-load-development-channels=server:rc"]
    assert not _is_attachable(plain)
    assert _is_attachable(shimmed)
    assert _is_attachable(joined)


def test_holder_assignment_prefers_the_session_a_process_names(tmp_path: Path) -> None:
    named = Holder(pid=1, identity=(1, "a"), session_id="sess", cwd=None)
    anonymous = Holder(pid=2, identity=(2, "b"), session_id=None, cwd=str(tmp_path))
    scan = HolderScan(holders=[named, anonymous], complete=True)
    assigned = scan.assign(
        [SessionRef("sess", cwd=str(tmp_path)), SessionRef("other", cwd=str(tmp_path))]
    )
    assert assigned == {"sess": named, "other": anonymous}


def test_holder_assignment_takes_the_process_a_bridge_already_registered() -> None:
    live = Holder(pid=7, identity=(7, "start"), session_id=None, cwd="/repo", attachable=True)
    scan = HolderScan(holders=[live], complete=True)
    assigned = scan.assign([SessionRef("sess", cwd="/repo", pid=7, identity=(7, "start"))])
    assert assigned == {"sess": live}
    # A pid the operating system handed to something else is not that process.
    recycled = scan.assign([SessionRef("sess", cwd="/repo", pid=7, identity=(7, "later"))])
    assert recycled == {}


def test_one_terminal_never_claims_every_session_in_its_directory(tmp_path: Path) -> None:
    """The reported defect: `claude` in a directory that holds older sessions.

    A session started from the shim carries no session id in its argv, so the
    working directory was the only thing left to match on and every session
    that shared it was told a terminal had taken it over.
    """
    cwd = str(tmp_path)
    shimmed = Holder(pid=100, identity=(100, "a"), session_id=None, cwd=cwd, attachable=True)
    scan = HolderScan(holders=[shimmed], complete=True)
    older = [SessionRef("old-1", cwd=cwd), SessionRef("old-2", cwd=cwd)]

    # Before its bridge registers, the new session is not a session yet.
    assert scan.assign(older) == {}
    # Once it has registered, its own pid claims it and the others stay free.
    assigned = scan.assign([SessionRef("live", cwd=cwd, pid=100), *older])
    assert assigned == {"live": shimmed}


def test_a_directory_with_several_candidates_is_left_alone(tmp_path: Path) -> None:
    cwd = str(tmp_path)
    anonymous = Holder(pid=2, identity=(2, "b"), session_id=None, cwd=cwd)
    one_process = HolderScan(holders=[anonymous], complete=True)
    assert one_process.assign([SessionRef("only", cwd=cwd)]) == {"only": anonymous}
    assert one_process.assign([SessionRef("a", cwd=cwd), SessionRef("b", cwd=cwd)]) == {}

    two_processes = HolderScan(
        holders=[anonymous, Holder(pid=3, identity=(3, "c"), session_id=None, cwd=cwd)],
        complete=True,
    )
    assert two_processes.assign([SessionRef("only", cwd=cwd)]) == {}


def claude_agent() -> AgentInfo:
    return AgentInfo(
        agent="claude",
        available=True,
        path="/bin/claude",
        capabilities=["takeover", "interrupt", "queue", "history"],
        attach="channel",
        attach_ready=True,
    )


def seed_session(registry: Registry, session_id: str, cwd: str, *, archived: bool) -> None:
    registry.upsert_session(
        Session(
            session_id=session_id,
            device_id="dev-1",
            agent="claude",
            cwd=cwd,
            state="idle",
            origin="terminal",
            control="none",
            archived=archived,
        )
    )


def seed_transcript(root: Path, session_id: str, cwd: str) -> TranscriptInfo:
    path = root / f"{session_id}.jsonl"
    write_rows(path, [user_row("hello", uuid=session_id)])
    stat = path.stat()
    return TranscriptInfo(
        session_id=session_id, path=str(path), cwd=cwd, size=stat.st_size, mtime=stat.st_mtime
    )


async def test_a_terminal_session_leaves_the_older_ones_in_its_directory_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reported defect, in the shape the user's own state database has it.

    Three Claude sessions share one working directory, two of them finished
    long ago and one of those archived. Starting `claude` there must publish
    the new session and nothing else.
    """
    cwd = str(tmp_path)
    root = tmp_path / "projects"
    root.mkdir()
    registry = Registry(tmp_path / "state.sqlite3")
    frames: list[dict[str, Any]] = []

    async def publish(frame: dict[str, Any]) -> None:
        frames.append(frame)

    hub = SessionHub(registry, publish, "dev-1", lambda: [claude_agent()])
    seed_session(registry, "old-1", cwd, archived=True)
    seed_session(registry, "old-2", cwd, archived=False)
    hub.load()

    # The terminal starts a session and its bridge registers, exactly as the
    # shim arranges; the CLI's own pid comes in with the registration.
    await hub.attach_registered(
        Attachment(session_id="live", cwd=cwd, pid=100, claude_version="2.1.267")
    )

    mirror = MirrorService(hub)
    found = [seed_transcript(root, name, cwd) for name in ("old-1", "old-2", "live")]
    for info in found:
        # These transcripts were mirrored before; only what is appended is new.
        registry.set_kv(mirror._offset_key(info.session_id), str(info.size))
    monkeypatch.setattr(claude_transcripts, "discover", lambda *args: found)
    monkeypatch.setattr(codex_rollouts, "discover", lambda *args: [])

    async def fake_scan() -> HolderScan:
        return HolderScan(
            holders=[
                Holder(pid=100, identity=(100, "a"), session_id=None, cwd=cwd, attachable=True)
            ],
            complete=True,
        )

    monkeypatch.setattr(mirror_module, "scan_holders", fake_scan)

    def touched() -> set[str]:
        return {
            frame.get("session_id") or frame.get("session", {}).get("session_id")
            for frame in frames
        }

    before = {name: hub.entry(name).session.updated_at for name in ("old-1", "old-2")}
    frames.clear()
    await mirror.scan_once()
    await mirror.tail_once()
    assert touched() == set()

    # The terminal types into the session it is actually running.
    write_rows(root / "live.jsonl", [user_row("what now", uuid="u2")])
    await mirror.tail_once()
    assert touched() == {"live"}

    for name in ("old-1", "old-2"):
        session = hub.entry(name).session
        assert session.updated_at == before[name]
        assert session.control == "none"
    assert hub.entry("old-1").session.state == "stopped"
    assert hub.entry("old-1").session.archived is True
    assert hub.entry("live").session.control == "shared"

    await hub.close()
    registry.close()


def meta_events(frames: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        frame["event"]
        for frame in frames
        if frame.get("type") == "session.event" and frame["event"].get("kind") == "meta"
    ]


def no_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_scan() -> HolderScan:
        return HolderScan(holders=[], complete=True)

    monkeypatch.setattr(mirror_module, "scan_holders", fake_scan)


async def test_a_mirrored_session_reports_the_settings_the_terminal_chose(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Amendment A17: the values come from the transcript, changes as `meta`."""
    cwd = str(tmp_path)
    root = tmp_path / "projects"
    root.mkdir()
    registry = Registry(tmp_path / "state.sqlite3")
    frames: list[dict[str, Any]] = []

    async def publish(frame: dict[str, Any]) -> None:
        frames.append(frame)

    hub = SessionHub(registry, publish, "dev-1", lambda: [claude_agent()])
    hub.load()
    path = root / "live.jsonl"
    write_rows(path, [model_row("claude-fable-5-1"), permission_row("auto"), effort_row("high")])
    stat = path.stat()
    found = [
        TranscriptInfo(
            session_id="live", path=str(path), cwd=cwd, size=stat.st_size, mtime=stat.st_mtime
        )
    ]
    monkeypatch.setattr(claude_transcripts, "discover", lambda *args: found)
    monkeypatch.setattr(codex_rollouts, "discover", lambda *args: [])
    no_terminal(monkeypatch)

    mirror = MirrorService(hub)
    # The mirror starts at the end of the file, so only the backfill can know
    # what was chosen before it was adopted.
    registry.set_kv(mirror._offset_key("live"), str(stat.st_size))
    await mirror.scan_once()
    assert [dict(event, seq=0, ts=0) for event in meta_events(frames)] == [
        {
            "seq": 0,
            "ts": 0,
            "kind": "meta",
            "model": "claude-fable-5-1",
            "permission_mode": "auto",
            "effort": "high",
        }
    ]
    session = hub.entry("live").session
    assert (session.model, session.permission_mode, session.effort) == (
        "claude-fable-5-1",
        "auto",
        "high",
    )

    # A turn that repeats the same permission mode changes nothing.
    frames.clear()
    write_rows(path, [permission_row("auto"), effort_row("high", text="still here")])
    await mirror.tail_once()
    assert meta_events(frames) == []

    # `/model` in the terminal: one `meta`, carrying that field alone.
    frames.clear()
    write_rows(path, [model_row("claude-opus-5[1m]")])
    await mirror.tail_once()
    assert [event["kind"] for event in meta_events(frames)] == ["meta"]
    assert meta_events(frames)[0].get("model") == "claude-opus-5[1m]"
    assert "permission_mode" not in meta_events(frames)[0]
    assert hub.entry("live").session.model == "claude-opus-5[1m]"

    await hub.close()
    registry.close()


async def test_a_session_this_device_drives_keeps_its_own_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The device set them over the SDK; its transcript must not re-report them."""
    cwd = str(tmp_path)
    root = tmp_path / "projects"
    root.mkdir()
    registry = Registry(tmp_path / "state.sqlite3")
    frames: list[dict[str, Any]] = []

    async def publish(frame: dict[str, Any]) -> None:
        frames.append(frame)

    hub = SessionHub(registry, publish, "dev-1", lambda: [claude_agent()])
    session = Session(
        session_id="driven",
        device_id="dev-1",
        agent="claude",
        cwd=cwd,
        state="idle",
        origin="remote",
        control="remote",
        model="opus",
    )
    entry = hub.register_mirrored(session)
    entry.runner = object()  # type: ignore[assignment]
    path = root / "driven.jsonl"
    write_rows(path, [model_row("claude-fable-5-1"), permission_row("auto")])
    stat = path.stat()
    found = [
        TranscriptInfo(
            session_id="driven", path=str(path), cwd=cwd, size=stat.st_size, mtime=stat.st_mtime
        )
    ]
    monkeypatch.setattr(claude_transcripts, "discover", lambda *args: found)
    monkeypatch.setattr(codex_rollouts, "discover", lambda *args: [])
    no_terminal(monkeypatch)

    mirror = MirrorService(hub)
    await mirror._backfill_known()
    await mirror.scan_once()
    await mirror.tail_once()
    assert meta_events(frames) == []
    assert hub.entry("driven").session.model == "opus"
    assert hub.entry("driven").session.permission_mode is None

    entry.runner = None
    await hub.close()
    registry.close()


async def test_a_closed_bridge_asks_after_its_own_process_not_the_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second `claude` started beside this one is not this session's CLI."""
    cwd = str(tmp_path)
    registry = Registry(tmp_path / "state.sqlite3")

    async def publish(frame: dict[str, Any]) -> None:
        return None

    hub = SessionHub(registry, publish, "dev-1", lambda: [claude_agent()])
    await hub.attach_registered(
        Attachment(session_id="live", cwd=cwd, pid=100, claude_version="2.1.267")
    )
    entry = hub.entry("live")

    def seen(found: list[Holder]) -> None:
        async def fake_scan() -> HolderScan:
            return HolderScan(holders=found, complete=True)

        monkeypatch.setattr(claude_holders, "scan_holders", fake_scan)

    mine = Holder(pid=100, identity=(100, "a"), session_id=None, cwd=cwd, attachable=True)
    neighbour = Holder(pid=200, identity=(200, "b"), session_id=None, cwd=cwd, attachable=True)

    seen([mine, neighbour])
    assert await hub._holder_control(entry) == "terminal"
    assert (entry.holder_pid, entry.holder_identity) == (100, (100, "a"))

    seen([neighbour])
    assert await hub._holder_control(entry) == "none"
    assert entry.holder_pid is None

    await hub.close()
    registry.close()
