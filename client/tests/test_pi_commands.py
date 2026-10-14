"""Amendment A27 for pi: the commands a session offers, and running one.

Three surfaces are covered here. `slash` maps pi's own list and reads the
prompt and skill directories the way pi reads them; `PiRunner` drives a real
fake `pi --mode rpc` child through `get_commands`, `prompt` and `compact`; and
`PiTerminalSession` does the same over the extension socket with the fake
extension of `test_pi_extension.py` on the other end.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from rc_client.agents.pi import frames as pi_frames
from rc_client.agents.pi import paths as pi_paths
from rc_client.agents.pi import plugin, slash
from rc_client.agents.pi import runtime as pi_runtime
from rc_client.errors import RcError
from rc_client.models import Session
from rc_client.registry import Registry
from rc_client.sessions.hub import SessionHub

from .test_pi_extension import Bench, FakeExtension, agent_info, connect, settle
from .test_pi_runner import SESSION, build, wait_for

# A request id, which is what the hub passes as a command's block id (A12).
REQUEST = "3f7b1c05-9d2e-4a86-b1f4-6c0e8a52d913"


@pytest.fixture
async def attached(tmp_path: Path, socket_dir: Path) -> AsyncIterator[Bench]:
    """A hub with a live extension socket in front of it, as `test_pi_extension`
    has one; named apart from that file's own fixture so both can be imported."""
    registry = Registry(tmp_path / "state.sqlite3")
    published: list[dict[str, Any]] = []

    async def publish(frame: dict[str, Any]) -> None:
        published.append(frame)

    hub = SessionHub(registry, publish, "dev-1", agent_info)
    made = Bench(hub, published, socket_dir / "pi-extension.sock")
    await made.server.start()
    try:
        yield made
    finally:
        await made.server.stop()
        await hub.close()


# ------------------------------------------------------------- pi's own list


def entry(name: str, source: str, path: str, description: str = "does a thing") -> dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "source": source,
        "sourceInfo": {"path": path, "source": "auto", "scope": "user", "origin": "top-level"},
    }


def test_pi_s_list_becomes_commands_with_the_device_s_compact_at_the_front() -> None:
    listed = slash.from_agent(
        [
            entry("llama", "extension", "<inline:llama.cpp>"),
            entry("release-notes", "prompt", "/nowhere/release-notes.md"),
            entry("skill:pdf-tables", "skill", "/nowhere/pdf-tables/SKILL.md"),
        ]
    )
    assert [(command.name, command.group) for command in listed] == [
        ("compact", "Built-in"),
        ("llama", "Extensions"),
        ("release-notes", "Prompts"),
        ("skill:pdf-tables", "Skills"),
    ]
    assert listed[0].argument == "instructions"
    # A command with no file of its own reports a placeholder, not a path.
    assert listed[1].argument is None


def test_a_prompt_template_s_argument_hint_is_read_back_off_the_disk(tmp_path: Path) -> None:
    """pi's list carries no hint, and the terminal shows one, so we fetch it."""
    template = tmp_path / "release-notes.md"
    template.write_text(
        '---\ndescription: Draft release notes\nargument-hint: "<tag>"\n---\nDraft them.\n',
        encoding="utf-8",
    )
    listed = slash.from_agent([entry("release-notes", "prompt", str(template))])
    assert listed[1].argument == "<tag>"


def test_a_name_no_app_could_send_back_is_not_offered() -> None:
    listed = slash.from_agent(
        [entry("Shouty", "prompt", "/nowhere/Shouty.md"), entry("fine", "prompt", "/nowhere/f.md")]
    )
    assert [command.name for command in listed] == ["compact", "fine"]


def test_a_paragraph_of_a_description_is_cut_to_one_row() -> None:
    listed = slash.from_agent(
        [entry("wordy", "skill", "/nowhere/SKILL.md", description="word " * 200)]
    )
    assert len(listed[1].description) == slash.MAX_DESCRIPTION
    assert listed[1].description.endswith("…")


def test_pi_s_own_compact_never_appears_twice() -> None:
    listed = slash.from_agent([entry("compact", "prompt", "/nowhere/compact.md")])
    assert [command.name for command in listed] == ["compact"]
    assert listed[0].group == "Built-in"


def test_a_list_that_is_not_a_list_still_offers_compact() -> None:
    assert [command.name for command in slash.from_agent(None)] == ["compact"]


def test_the_typed_text_is_what_the_bubble_shows() -> None:
    assert slash.typed("compact", None) == "/compact"
    assert slash.typed("compact", "keep the failures") == "/compact keep the failures"


# ------------------------------------------------------------------- on disk


def write_prompt(directory: Path, name: str, body: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{name}.md").write_text(body, encoding="utf-8")


def write_skill(directory: Path, body: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "SKILL.md").write_text(body, encoding="utf-8")


def test_the_global_prompts_and_skills_are_what_a_resumed_session_would_have() -> None:
    home = pi_runtime.home()
    write_prompt(
        home / "agent" / "prompts",
        "release-notes",
        '---\ndescription: Draft release notes\nargument-hint: "<tag>"\n---\nDraft them.\n',
    )
    write_skill(
        home / "agent" / "skills" / "pdf-tables",
        "---\nname: pdf-tables\ndescription: Extract tables from a PDF into CSV\n---\nDo it.\n",
    )
    write_skill(
        home.parent / ".agents" / "skills" / "grouped" / "browse",
        "---\nname: browse\ndescription: Drive a browser\n---\nDo it.\n",
    )
    listed = slash.offline()
    assert [(command.name, command.group) for command in listed] == [
        ("compact", "Built-in"),
        ("release-notes", "Prompts"),
        ("skill:pdf-tables", "Skills"),
        ("skill:browse", "Skills"),
    ]
    assert listed[1].argument == "<tag>"
    assert listed[2].description == "Extract tables from a PDF into CSV"


def test_a_template_with_no_description_is_described_by_its_first_line() -> None:
    write_prompt(pi_runtime.home() / "agent" / "prompts", "fixup", "\nFix the failing tests.\n")
    assert slash.offline()[1].description == "Fix the failing tests."


def test_a_skill_with_no_description_is_not_a_skill() -> None:
    empty = pi_runtime.home() / "agent" / "skills" / "empty"
    write_skill(empty, "---\nname: empty\n---\nNothing.\n")
    assert [command.name for command in slash.offline()] == ["compact"]


def test_a_directory_holding_a_skill_is_not_descended_into() -> None:
    root = pi_runtime.home() / "agent" / "skills" / "outer"
    write_skill(root, "---\nname: outer\ndescription: The outer one\n---\nDo it.\n")
    write_skill(root / "inner", "---\nname: inner\ndescription: The inner one\n---\nDo it.\n")
    assert [command.name for command in slash.offline()] == ["compact", "skill:outer"]


def test_a_project_s_own_prompts_are_never_promised(tmp_path: Path) -> None:
    """`--no-approve` makes pi ignore them, so listing them would be a lie."""
    write_prompt(tmp_path / ".pi" / "prompts", "local", "---\ndescription: Local\n---\nGo.\n")
    assert [command.name for command in slash.offline()] == ["compact"]


async def test_the_plugin_answers_for_a_session_with_no_process(tmp_path: Path) -> None:
    assert "commands" in plugin.CAPABILITIES
    prompts = pi_runtime.home() / "agent" / "prompts"
    write_prompt(prompts, "release-notes", "---\ndescription: Notes\n---\nGo.\n")
    session = Session(session_id=SESSION, device_id="d1", agent="pi", cwd=str(tmp_path))
    listed = await plugin.commands(session)
    assert [command.name for command in listed] == ["compact", "release-notes"]


# ------------------------------------------------------------- the RPC child


async def test_a_running_session_lists_what_pi_says_it_has(tmp_path: Path) -> None:
    runner, _recorder, peer = build(tmp_path, "turn")
    await runner.start()
    listed = await runner.commands()
    await runner.close()
    assert "get_commands" in peer.names()
    assert [(command.name, command.group) for command in listed] == [
        ("compact", "Built-in"),
        ("llama", "Extensions"),
        ("release-notes", "Prompts"),
        ("skill:pdf-tables", "Skills"),
    ]


async def test_a_session_whose_process_is_gone_falls_back_to_the_disk(tmp_path: Path) -> None:
    runner, _recorder, _peer = build(tmp_path, "turn")
    assert [command.name for command in await runner.commands()] == ["compact"]


async def test_a_command_pi_does_not_offer_is_not_found(tmp_path: Path) -> None:
    runner, _recorder, _peer = build(tmp_path, "turn")
    await runner.start()
    with pytest.raises(RcError) as caught:
        await runner.command("dance", None, REQUEST)
    await runner.close()
    assert caught.value.code == "not_found"


async def test_a_prompt_template_goes_to_pi_as_the_text_of_the_turn(tmp_path: Path) -> None:
    """pi expands it before the turn, so the bubble keeps what was typed."""
    runner, recorder, peer = build(tmp_path, "turn")
    await runner.start()
    await runner.command("release-notes", "v1.2", REQUEST)
    await wait_for(lambda: recorder.events("turn_completed"))
    await runner.close()

    assert peer.of("prompt")[0]["message"] == "/release-notes v1.2"
    echoes = recorder.events("user_message")
    assert [(echo["block_id"], echo["text"]) for echo in echoes] == [
        (REQUEST, "/release-notes v1.2")
    ]
    assert echoes[0]["source"] == "remote"
    # The turn is pi's to open: an extension command runs without one at all.
    assert recorder.events("turn_completed")[0]["stop_reason"] == "completed"


async def test_compact_is_pi_s_own_command_and_its_notice_is_published(tmp_path: Path) -> None:
    runner, recorder, peer = build(tmp_path, "turn")
    await runner.start()
    await runner.command("compact", "keep the failures", REQUEST)
    await wait_for(lambda: recorder.events("notice"))
    await runner.close()

    assert peer.of("compact")[0]["customInstructions"] == "keep the failures"
    assert recorder.events("user_message")[0]["text"] == "/compact keep the failures"
    assert recorder.events("notice")[0]["text"] == (
        "Context was compacted; earlier turns are summarised."
    )
    assert peer.of("prompt") == []


async def test_a_refused_compaction_is_the_reply_and_not_a_second_notice(tmp_path: Path) -> None:
    runner, recorder, _peer = build(tmp_path, "refuse")
    await runner.start()
    with pytest.raises(RcError) as caught:
        await runner.command("compact", None, REQUEST)
    # Long enough for the refusal's own event to have been handled if it were.
    for _ in range(50):
        await asyncio.sleep(0.002)
    await runner.close()

    assert caught.value.code == "bad_request"
    assert caught.value.message == "Nothing to compact (session too small)"
    assert recorder.events("notice") == []
    assert recorder.events("user_message")[0]["text"] == "/compact"


# -------------------------------------------------------- the attached session


async def answer(extension: FakeExtension, data: dict[str, Any] | None) -> dict[str, Any]:
    return await extension.serve_command(data)


async def test_an_attached_session_lists_what_the_extension_reads(attached: Bench) -> None:
    extension = await connect(attached)
    await extension.hello()
    await settle()
    runner = attached.runner()

    listing = asyncio.create_task(runner.commands())
    frame = await answer(
        extension,
        {"commands": [entry("skill:pdf-tables", "skill", "/nowhere/SKILL.md")]},
    )
    listed = await listing
    extension.close()

    assert frame["command"] == pi_frames.COMMANDS
    assert [command.name for command in listed] == ["compact", "skill:pdf-tables"]


async def test_an_attached_command_is_sent_expanded_and_echoed_here(attached: Bench) -> None:
    """An extension command raises no `input`, so the bubble cannot wait for one."""
    extension = await connect(attached)
    await extension.hello()
    await settle()
    runner = attached.runner()

    running = asyncio.create_task(runner.command("release-notes", "v1.2", REQUEST))
    listing = await answer(
        extension, {"commands": [entry("release-notes", "prompt", "/nowhere/n.md")]}
    )
    sent = await answer(extension, None)
    await running
    extension.close()

    assert listing["command"] == pi_frames.COMMANDS
    assert sent["command"] == pi_frames.SEND
    assert sent["text"] == "/release-notes v1.2"
    assert sent["expand"] is True
    assert sent["echo"] is False
    echoes = attached.events("user_message")
    assert [(echo["block_id"], echo["text"]) for echo in echoes] == [
        (REQUEST, "/release-notes v1.2")
    ]


async def test_an_attached_compact_goes_through_the_extension(attached: Bench) -> None:
    extension = await connect(attached)
    await extension.hello()
    await settle()
    runner = attached.runner()

    running = asyncio.create_task(runner.command("compact", "keep the failures", REQUEST))
    await answer(extension, {"commands": []})
    compacting = await answer(extension, None)
    await running
    extension.close()

    assert compacting["command"] == pi_frames.COMPACT
    assert compacting["instructions"] == "keep the failures"
    assert attached.events("user_message")[0]["text"] == "/compact keep the failures"


async def test_an_attached_session_refuses_a_command_it_does_not_offer(attached: Bench) -> None:
    extension = await connect(attached)
    await extension.hello()
    await settle()
    runner = attached.runner()

    running = asyncio.create_task(runner.command("dance", None, REQUEST))
    await answer(extension, {"commands": []})
    with pytest.raises(RcError) as caught:
        await running
    extension.close()
    assert caught.value.code == "not_found"


# ------------------------------------------------------------- the TS itself


def test_the_extension_answers_every_command_the_device_sends() -> None:
    """Both ends of this socket are ours, so they are checked against each other."""
    source = pi_paths.bundled_extension().read_text(encoding="utf-8")
    for command in (
        pi_frames.SEND,
        pi_frames.ABORT,
        pi_frames.SET_MODEL,
        pi_frames.SET_THINKING,
        pi_frames.SET_PERMISSION_MODE,
        pi_frames.STATS,
        pi_frames.COMMANDS,
        pi_frames.COMPACT,
    ):
        assert f'command === "{command}"' in source or f'"{command}"' in source, command
    # The two calls A27 needs from pi's extension API, which is what makes an
    # attached session as capable as an RPC one.
    assert "pi.getCommands()" in source
    assert "ctx.compact(" in source
