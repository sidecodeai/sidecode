"""The words that steer the two models: what the rules about waiting and about sessions say."""

from __future__ import annotations

from rc_gateway.live.config import LiveConfig
from rc_gateway.live.prompts import BACKEND_INSTRUCTIONS, VOICE_INSTRUCTIONS, session_config
from rc_gateway.live.tool_specs import TOOLS


def test_the_voice_model_acknowledges_briefly_before_it_delegates_and_then_waits_quietly() -> None:
    policy = VOICE_INSTRUCTIONS.split("Delegation policy:")[1]
    assert "<|SpawnThinking|>, first say one very short acknowledgement" in policy
    assert "developer's language" in policy
    assert '"Let me check."' in policy
    assert "wait quietly" in policy
    assert "no filler at all while the developer is talking" in policy


def test_the_voice_model_is_told_that_this_session_needs_no_clarifying_question() -> None:
    flattened = " ".join(VOICE_INSTRUCTIONS.split())
    assert "the backend knows which session is open on the developer's screen" in flattened
    assert 'so "this session" is a clear request' in flattened


def test_the_backend_passes_ids_to_tools_and_never_says_one_aloud() -> None:
    assert "by its id, exactly as list_sessions returned it" in BACKEND_INSTRUCTIONS
    assert "ambiguous" in BACKEND_INSTRUCTIONS
    assert "never say one aloud" in BACKEND_INSTRUCTIONS
    assert "never by id" in BACKEND_INSTRUCTIONS  # it still names sessions by project and title


def test_the_backend_reads_this_session_as_the_entry_marked_on_screen() -> None:
    assert '"This session"' in BACKEND_INSTRUCTIONS
    assert "on_screen: true" in BACKEND_INSTRUCTIONS
    assert "ask which session is meant" in BACKEND_INSTRUCTIONS


def test_every_tool_that_takes_a_session_asks_for_its_id() -> None:
    described = {
        str(tool["name"]): tool["parameters"]["properties"]["session"]["description"]
        for tool in TOOLS
        if "session" in tool["parameters"]["properties"]
    }
    assert set(described) == {
        "get_session_status",
        "get_session_context",
        "prepare_message",
    }
    assert all("id" in text and "list_sessions" in text for text in described.values())


def test_the_session_carries_both_prompts() -> None:
    session = session_config(LiveConfig(api_key="k"))
    assert session["instructions"] == VOICE_INSTRUCTIONS
    assert session["delegation"]["responses"]["instructions"] == BACKEND_INSTRUCTIONS
