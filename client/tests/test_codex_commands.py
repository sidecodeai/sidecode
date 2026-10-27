"""Amendment A27: the slash commands a Codex session lists and runs.

The runner side is exercised against `fake_codex_daemon.py`, which speaks the
real framing over a real socket; the formatting side is exercised against
payloads shaped like the ones the real daemon answers.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from rc_client.agents.codex import commands as slash
from rc_client.agents.codex import plugin as codex_plugin
from rc_client.agents.codex import reports
from rc_client.agents.codex.adapter import CodexRunner
from rc_client.agents.codex.daemon.rpc import DaemonClient
from rc_client.agents.codex.daemon.session import CodexDaemonSession
from rc_client.agents.codex.init_prompt import INIT_PROMPT
from rc_client.agents.codex.models import ModelCatalog
from rc_client.agents.codex.translate import CodexTranslator
from rc_client.errors import RcError
from rc_client.ids import block_uuid
from rc_client.models import Choice, Session
from rc_client.registry import Registry
from rc_client.sessions.channel import SessionChannel
from tests.fake_codex_daemon import FakeDaemon
from tests.helpers import event_validator, object_validator

THREAD = "01a09c9f-3fda-7330-ae2f-911637977c97"
REQUEST = "5c1d7e2a-9b3f-4a8c-8d6e-0f1a2b3c4d5e"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "codex"


def fixture(name: str) -> Any:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


class Recorder:
    def __init__(self) -> None:
        self.frames: list[dict[str, Any]] = []

    async def __call__(self, frame: dict[str, Any]) -> None:
        self.frames.append(frame)

    def events(self, kind: str) -> list[dict[str, Any]]:
        return [
            frame["event"]
            for frame in self.frames
            if frame.get("type") == "session.event" and frame["event"]["kind"] == kind
        ]


class Bench:
    """One `CodexDaemonSession` on a real socket, with what it published."""

    def __init__(self, session: CodexDaemonSession, recorder: Recorder, daemon: FakeDaemon) -> None:
        self.session = session
        self.recorder = recorder
        self.daemon = daemon

    def events(self, kind: str) -> list[dict[str, Any]]:
        return self.recorder.events(kind)

    def block(self, kind: str) -> dict[str, Any]:
        found = self.events(kind)
        assert found, f"no {kind} event was published"
        return found[-1]


@pytest.fixture
async def bench(tmp_path: Path, socket_dir: Path) -> Any:
    daemon = FakeDaemon(socket_dir / "codex.sock")
    await daemon.start()
    daemon.replies["thread/resume"] = {
        "thread": {"id": THREAD},
        "model": "gpt-5.6-sol",
        "reasoningEffort": "medium",
        "approvalPolicy": "on-request",
        "sandbox": {"type": "workspaceWrite", "writableRoots": [], "networkAccess": True},
    }
    daemon.replies["thread/items/list"] = {"data": []}
    daemon.replies["turn/start"] = {"turn": {"id": "turn-1"}}
    registry = Registry(tmp_path / "state.sqlite3")
    session = Session(
        session_id=THREAD, device_id="dev-1", agent="codex", cwd="/repo", control="remote"
    )
    registry.upsert_session(session)
    recorder = Recorder()
    channel = SessionChannel(registry, session, recorder)
    catalog = ModelCatalog(models=[Choice("gpt-5.6-sol", "Sol")], default_model="gpt-5.6-sol")
    client = DaemonClient(
        "0.1.0",
        on_notification=_ignore_notification,
        on_request=_ignore_request,
        socket=daemon.path,
    )
    await client.start()
    runner = CodexDaemonSession(channel, client, cwd="/repo", catalog=catalog, thread_id=THREAD)
    await runner.start()
    try:
        yield Bench(runner, recorder, daemon)
    finally:
        await runner.close()
        await client.close()
        await daemon.stop()
        await channel.close()
        registry.close()


async def _ignore_notification(method: str, params: dict[str, Any]) -> None:
    return None


async def _ignore_request(request_id: Any, method: str, params: dict[str, Any]) -> Any:
    return {}


async def settle(check: Any, timeout: float = 2.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if check():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition never became true")


# ------------------------------------------------------------------- the table


def test_the_table_is_what_the_apps_are_offered() -> None:
    listed = slash.listing()
    assert [command.name for command in listed] == [
        "compact",
        "review",
        "init",
        "diff",
        "status",
        "usage",
        "skills",
        "hooks",
        "mcp",
    ]
    assert all(command.description for command in listed)
    # One source, so nothing carries a group; only `/review` takes an argument.
    assert {command.group for command in listed} == {None}
    assert [command.name for command in listed if command.argument] == ["review"]


def test_settings_and_lifecycle_are_never_commands() -> None:
    """A device that listed these would give a phone two controls for one thing."""
    listed = {command.name for command in slash.listing()}
    for name in ("model", "permissions", "fast", "name", "new", "archive", "delete", "resume"):
        assert name not in listed
    for name in ("vim", "theme", "keymap", "copy", "pets", "raw", "cd", "pwd"):
        assert name not in listed


def test_codex_carries_the_capability_and_answers_without_a_process() -> None:
    assert "commands" in codex_plugin.CAPABILITIES


async def test_a_session_with_no_process_offers_the_same_table() -> None:
    session = Session(session_id=THREAD, device_id="dev-1", agent="codex", cwd="/repo")
    offline = await codex_plugin.commands(session)
    assert [command.name for command in offline] == [command.name for command in slash.listing()]


def test_the_init_prompt_is_the_one_the_terminal_sends() -> None:
    """Read out of the Codex 0.154.0 binary; a paraphrase writes another file."""
    assert INIT_PROMPT.startswith("Generate a file named AGENTS.md")
    assert '- Title the document "Repository Guidelines".' in INIT_PROMPT
    assert "Commit & Pull Request Guidelines" in INIT_PROMPT


# ---------------------------------------------------------------- the running


async def test_an_unknown_name_is_not_found(bench: Bench) -> None:
    with pytest.raises(RcError) as caught:
        await bench.session.command("dance", None, REQUEST)
    assert caught.value.code == "not_found"
    assert bench.events("user_message") == []


async def test_compact_echoes_the_command_and_asks_the_daemon(bench: Bench) -> None:
    await bench.session.command("compact", None, REQUEST)
    echo = bench.block("user_message")
    assert echo["block_id"] == REQUEST
    assert echo["text"] == "/compact"
    assert echo["source"] == "remote"
    assert bench.daemon.sent("thread/compact/start")[-1] == {"threadId": THREAD}


async def test_review_without_an_argument_reviews_the_working_tree(bench: Bench) -> None:
    await bench.session.command("review", None, REQUEST)
    assert bench.block("user_message")["text"] == "/review"
    sent = bench.daemon.sent("review/start")[-1]
    assert sent["target"] == {"type": "uncommittedChanges"}
    assert sent["delivery"] == "inline"
    assert sent["threadId"] == THREAD


async def test_review_with_an_argument_becomes_the_reviewer_s_brief(bench: Bench) -> None:
    await bench.session.command("review", "focus on the retry logic", REQUEST)
    assert bench.block("user_message")["text"] == "/review focus on the retry logic"
    sent = bench.daemon.sent("review/start")[-1]
    assert sent["target"] == {"type": "custom", "instructions": "focus on the retry logic"}


async def test_init_sends_the_canned_prompt_under_a_bubble_that_says_init(bench: Bench) -> None:
    await bench.session.command("init", None, REQUEST)
    echo = bench.block("user_message")
    assert echo["block_id"] == REQUEST
    assert echo["text"] == "/init"
    sent = bench.daemon.sent("turn/start")[-1]
    assert sent["input"][0]["text"] == INIT_PROMPT


async def test_the_daemon_s_echo_of_the_init_prompt_is_not_a_second_bubble(
    bench: Bench,
) -> None:
    """Codex replays the prompt it was given; only `/init` belongs on screen."""
    await bench.session.command("init", None, REQUEST)
    await bench.daemon.echo_prompt(THREAD, INIT_PROMPT)
    for method in ("item/started", "item/completed"):
        await bench.session.notification(
            method,
            {
                "threadId": THREAD,
                "item": {
                    "type": "userMessage",
                    "id": "item-1",
                    "clientId": None,
                    "content": [{"type": "text", "text": INIT_PROMPT}],
                },
            },
        )
    texts = [event["text"] for event in bench.events("user_message")]
    assert texts == ["/init"]


async def test_status_reports_the_thread_without_asking_for_it_again(bench: Bench) -> None:
    bench.daemon.replies["thread/read"] = {"thread": {"id": THREAD, "name": "Typecheck the app"}}
    await bench.session.command("status", None, REQUEST)
    assert bench.block("user_message")["text"] == "/status"
    block = bench.block("tool_call")
    # The block is the command's own, not the bubble's, so both survive (§5.1).
    assert block["block_id"] == block_uuid(f"command:{REQUEST}")
    assert block["block_id"] != REQUEST
    assert block["tool"] == "/status" and block["title"] == "/status"
    assert block["tool_kind"] == "other" and block["status"] == "succeeded"
    assert "Model: gpt-5.6-sol" in block["output"]
    assert "Sandbox: workspace-write, network access" in block["output"]
    assert "Directory: /repo" in block["output"]
    assert "Thread: Typecheck the app" in block["output"]
    # The settings came back on `thread/resume`, so nothing was read again.
    assert bench.daemon.sent("config/read") == []


async def test_a_report_opens_as_running_before_it_answers(bench: Bench) -> None:
    bench.daemon.replies["thread/read"] = {"thread": {"id": THREAD}}
    await bench.session.command("status", None, REQUEST)
    statuses = [event["status"] for event in bench.events("tool_call")]
    assert statuses == ["running", "succeeded"]


async def test_status_falls_back_to_the_configuration_for_an_unknown_sandbox(
    bench: Bench,
) -> None:
    bench.session._sandbox = None
    bench.daemon.replies["thread/read"] = {"thread": {"id": THREAD}}
    bench.daemon.replies["config/read"] = {"config": {"sandbox_mode": "danger-full-access"}}
    await bench.session.command("status", None, REQUEST)
    assert "Sandbox: danger-full-access" in bench.block("tool_call")["output"]


async def test_usage_reads_both_account_calls(bench: Bench) -> None:
    bench.daemon.replies["account/usage/read"] = fixture("account_usage")
    bench.daemon.replies["account/rateLimits/read"] = fixture("rate_limits")
    await bench.session.command("usage", None, REQUEST)
    block = bench.block("tool_call")
    assert block["title"] == "/usage"
    assert "Plan: plus" in block["output"]
    assert "5-hour limit: 25% used" in block["output"]
    assert block["summary"] == "Plan: plus"


async def test_skills_hooks_and_mcp_each_become_one_block(bench: Bench) -> None:
    bench.daemon.replies["skills/list"] = fixture("skills_list")
    bench.daemon.replies["hooks/list"] = fixture("hooks_list")
    bench.daemon.replies["mcpServerStatus/list"] = fixture("mcp_status")

    await bench.session.command("skills", None, REQUEST)
    assert "3 skills available" in bench.block("tool_call")["output"]
    assert bench.daemon.sent("skills/list")[-1] == {"cwds": ["/repo"]}

    await bench.session.command("hooks", None, "req-2")
    assert "2 hooks configured" in bench.block("tool_call")["output"]

    await bench.session.command("mcp", None, "req-3")
    block = bench.block("tool_call")
    assert "3 MCP servers" in block["output"]
    assert bench.daemon.sent("mcpServerStatus/list")[-1] == {
        "threadId": THREAD,
        "detail": "toolsAndAuthOnly",
    }


async def test_diff_runs_git_beside_the_thread_rather_than_in_it(bench: Bench) -> None:
    outputs = {
        (
            "git",
            "diff",
            "--stat",
        ): " calc.py | 2 +-\n 1 file changed, 1 insertion(+), 1 deletion(-)\n",
        ("git", "diff"): "diff --git a/calc.py b/calc.py\n-    return a + b\n+    return a - b\n",
        ("git", "ls-files", "--others", "--exclude-standard"): "notes.md\n",
    }

    def responder(method: str, params: dict[str, Any]) -> Any:
        if method != "command/exec":
            return None
        key = tuple(params.get("command") or [])
        return {"exitCode": 0, "stdout": outputs.get(key, ""), "stderr": ""}

    bench.daemon.responder = responder
    await bench.session.command("diff", None, REQUEST)
    block = bench.block("tool_call")
    assert "1 file changed" in block["output"]
    assert "return a - b" in block["output"]
    assert "notes.md" in block["output"]
    assert bench.daemon.sent("command/exec")[0]["cwd"] == "/repo"
    # `thread/shellCommand` would have opened a turn and shown up as the agent's work.
    assert bench.daemon.sent("thread/shellCommand") == []


async def test_diff_outside_a_repository_says_so(bench: Bench) -> None:
    def responder(method: str, params: dict[str, Any]) -> Any:
        if method != "command/exec":
            return None
        return {"exitCode": 128, "stdout": "", "stderr": "not a git repository"}

    bench.daemon.responder = responder
    await bench.session.command("diff", None, REQUEST)
    assert bench.block("tool_call")["output"] == "not inside a git repository"


async def test_every_block_a_command_emits_matches_the_event_schema(bench: Bench) -> None:
    validator = event_validator()
    if validator is None:
        pytest.skip("the protocol schema is not present")
    bench.daemon.replies["thread/read"] = {"thread": {"id": THREAD}}
    bench.daemon.replies["account/usage/read"] = fixture("account_usage")
    bench.daemon.replies["account/rateLimits/read"] = fixture("rate_limits")
    await bench.session.command("status", None, REQUEST)
    await bench.session.command("usage", None, "req-2")
    await bench.session.command("compact", None, "req-3")
    for frame in bench.recorder.frames:
        if frame.get("type") == "session.event":
            validator.validate(frame["event"])


async def test_a_shared_session_marks_the_echo_delivered(bench: Bench) -> None:
    """A12: a bubble on a session a terminal also holds is already in the thread."""
    bench.session.channel.session.control = "shared"
    await bench.session.command("compact", None, REQUEST)
    assert bench.block("user_message")["delivery"] == "delivered"


# ----------------------------------------------------------- the translations


def _emits(translator: CodexTranslator, item: dict[str, Any], completed: bool) -> list[Any]:
    return translator.item(item, completed)


def test_a_compaction_is_a_notice_rather_than_a_tool_call() -> None:
    translator = CodexTranslator(cwd="/repo")
    item = {"type": "contextCompaction", "id": "cmp-1"}
    assert _emits(translator, item, completed=False) == []
    emits = _emits(translator, item, completed=True)
    assert [emit.kind for emit in emits] == ["notice"]
    assert emits[0].fields == {
        "level": "info",
        "text": "Context was compacted; earlier turns are summarised.",
    }


def test_a_review_opens_and_closes_with_a_notice() -> None:
    translator = CodexTranslator(cwd="/repo")
    entered = translator.item({"type": "enteredReviewMode", "id": "r-1", "review": "changes"}, True)
    assert [emit.fields["text"] for emit in entered] == ["Review started"]
    exited = translator.item({"type": "exitedReviewMode", "id": "r-2", "review": "…"}, True)
    assert [emit.fields["text"] for emit in exited] == ["Review finished"]


def test_the_reviewer_s_brief_is_not_a_bubble() -> None:
    """`review/start` feeds the reviewer a `userMessage` nobody typed."""
    translator = CodexTranslator(cwd="/repo", mirror_user_messages=True)
    brief = {"type": "userMessage", "id": "u-1", "content": [{"type": "text", "text": "Review …"}]}
    translator.item({"type": "enteredReviewMode", "id": "r-1", "review": "changes"}, False)
    assert translator.item(brief, True) == []
    translator.item({"type": "exitedReviewMode", "id": "r-2", "review": "…"}, True)
    after = translator.item({**brief, "id": "u-2"}, True)
    assert [emit.kind for emit in after] == ["user_message"]


# ------------------------------------------------------------- the formatting


def test_status_text_reads_as_the_terminal_s_panel_does() -> None:
    facts = reports.ThreadFacts(
        cwd="/repo",
        model="gpt-5.6-sol",
        effort="medium",
        speed=None,
        permission_mode="on-request",
        sandbox={"type": "workspaceWrite", "writableRoots": ["/tmp/out"], "networkAccess": True},
        usage={"total_tokens": 152987, "context_window": 258400},
    )
    text = reports.status_text(facts, "Typecheck the app")
    assert text.splitlines() == [
        "Model: gpt-5.6-sol",
        "Reasoning: medium",
        "Speed: standard",
        "Approvals: on-request",
        "Sandbox: workspace-write, network access, also writable: /tmp/out",
        "Directory: /repo",
        "Thread: Typecheck the app",
        "Tokens: 152,987 of 258,400 used (59%)",
    ]


def test_a_session_that_has_not_spent_a_token_says_so() -> None:
    assert reports.tokens_line({}) == "none used yet"
    assert reports.tokens_line({"total_tokens": 400}) == "400 used"


def test_an_unknown_sandbox_is_never_reported_as_permissive() -> None:
    assert reports.sandbox_label(None) == "unknown"
    assert reports.sandbox_label({"type": "readOnly"}) == "read-only"
    assert reports.sandbox_label({"type": "dangerFullAccess"}) == "danger-full-access"


def test_usage_text_names_each_window_the_way_the_account_reports_it() -> None:
    text = reports.usage_text(fixture("account_usage"), fixture("rate_limits"))
    lines = text.splitlines()
    assert lines[0] == "Plan: plus"
    assert lines[1].startswith("5-hour limit: 25% used, resets ")
    assert lines[2].startswith("Weekly limit: 40% used, resets ")
    assert "Limit resets available: 2" in text
    assert "Lifetime tokens: 1,234,567,890" in text
    assert "Current streak: 12 days" in text


def test_an_account_with_nothing_to_report_still_answers() -> None:
    assert reports.usage_text({}, {}) == "no account usage reported"


def test_skills_text_names_the_scope_and_the_disabled_ones() -> None:
    text = reports.skills_text(fixture("skills_list")["data"])
    assert text.splitlines()[0] == "3 skills available"
    assert "pdf-tables [user] — Extract tables from a PDF into CSV" in text
    assert "release-notes (disabled) [repo]" in text
    assert "missing a description" in text


def test_hooks_text_says_what_runs_and_whether_it_is_trusted() -> None:
    text = reports.hooks_text(fixture("hooks_list")["data"])
    assert text.splitlines()[0] == "2 hooks configured"
    assert 'preToolUse — command "/bin/sh" "/home/dev/.codex/hooks/guard.sh" (user)' in text
    assert "postToolUse — mcp lint.check (matches edit.*, untrusted, disabled, repo)" in text


def test_mcp_text_counts_the_tools_and_flags_the_broken_servers() -> None:
    text = reports.mcp_text(fixture("mcp_status")["data"])
    assert text.splitlines()[0] == "3 MCP servers"
    assert "context7 — connected, 2 tools" in text
    assert "calendar — authenticationRequired, 0 tools, not signed in" in text
    assert "history — failed, 0 tools — handshaking with MCP server failed" in text


def test_empty_lists_read_as_sentences_rather_than_zeroes() -> None:
    assert reports.skills_text([]) == "no skills are available here"
    assert reports.hooks_text([]) == "no hooks are configured here"
    assert reports.mcp_text([]) == "no MCP servers are configured"
    assert reports.diff_text("", "", []) == "the working tree is clean"


def test_a_long_list_is_cut_with_a_count_of_what_is_left() -> None:
    many = [
        {"cwd": "/repo", "skills": [{"name": f"s{index}", "enabled": True} for index in range(250)]}
    ]
    text = reports.skills_text(many)
    assert text.splitlines()[0] == "250 skills available"
    assert text.splitlines()[-1] == "  … and 50 more"


def test_diff_summary_is_the_line_the_app_shows_beside_the_block() -> None:
    stat = " calc.py | 2 +-\n 1 file changed, 1 insertion(+), 1 deletion(-)\n"
    assert reports.diff_summary(stat, []) == "1 file changed, 1 insertion(+), 1 deletion(-)"
    assert reports.diff_summary("", ["a.py", "b.py"]) == "2 untracked"


# ------------------------------------------------------ the private app-server


class FakeServer:
    """Enough of `CodexAppServer` for the commands: it records and answers."""

    def __init__(self, replies: dict[str, Any] | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.replies = replies or {}

    async def request(
        self, method: str, params: dict[str, Any], timeout: float = 60.0
    ) -> dict[str, Any]:
        self.calls.append((method, params))
        return dict(self.replies.get(method, {}))

    def sent(self, method: str) -> list[dict[str, Any]]:
        return [params for name, params in self.calls if name == method]


def embedded(tmp_path: Path) -> tuple[CodexRunner, Recorder, FakeServer, Registry]:
    registry = Registry(tmp_path / "state.sqlite3")
    session = Session(session_id="s1", device_id="d1", agent="codex", cwd="/repo")
    registry.upsert_session(session)
    recorder = Recorder()
    channel = SessionChannel(registry, session, recorder)
    runner = CodexRunner(channel, binary="/bin/codex", cwd="/repo", catalog=ModelCatalog())
    server = FakeServer({"thread/read": {"thread": {"id": THREAD}}})
    runner._server = server  # type: ignore[assignment]
    runner._thread_id = THREAD
    runner._model = "gpt-5.6-sol"
    runner._sandbox = {"type": "readOnly"}
    return runner, recorder, server, registry


async def test_a_session_on_its_own_app_server_runs_the_same_table(tmp_path: Path) -> None:
    """The private app-server and the shared daemon differ only in the client."""
    runner, recorder, server, registry = embedded(tmp_path)
    try:
        assert [command.name for command in await runner.commands()] == [
            command.name for command in slash.listing()
        ]
        await runner.command("status", None, REQUEST)
        assert recorder.events("user_message")[-1]["text"] == "/status"
        assert "Sandbox: read-only" in recorder.events("tool_call")[-1]["output"]

        await runner.command("init", None, "req-2")
        assert recorder.events("user_message")[-1]["text"] == "/init"
        assert server.sent("turn/start")[-1]["input"][0]["text"] == INIT_PROMPT

        with pytest.raises(RcError) as caught:
            await runner.command("dance", None, "req-3")
        assert caught.value.code == "not_found"
    finally:
        registry.close()


async def test_status_asks_the_thread_for_what_a_refused_resume_never_told_it(
    bench: Bench,
) -> None:
    """A thread with no rollout yet cannot be resumed, so its settings are unknown."""
    bench.session._model = None
    bench.session._effort = None
    bench.daemon.replies["thread/read"] = {
        "thread": {"id": THREAD, "model": "gpt-5.6-sol", "reasoningEffort": "high"}
    }
    await bench.session.command("status", None, REQUEST)
    block = bench.block("tool_call")
    assert "Model: gpt-5.6-sol" in block["output"]
    assert "Reasoning: high" in block["output"]
    assert block["summary"] == "gpt-5.6-sol"


def test_every_command_validates_against_the_protocol_schema() -> None:
    validator = object_validator("Command")
    if validator is None:
        pytest.skip("the protocol schema is not present")
    for command in slash.listing():
        validator.validate(command.to_dict())
