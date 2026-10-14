"""Amendments A18 and A48: which Codex threads the device may show.

The originator/source pairs below were read off a real machine, from
`thread/list` on Codex 0.154 and from the `session_meta` of the rollouts on
disk. Codex 0.160 terminal TUIs on an app-server also report `codex-tui/vscode`.
"""

from __future__ import annotations

import pytest

from rc_client.agents.codex.provenance import (
    DAEMON_CLIENT_NAME,
    EMBEDDED_CLIENT_NAME,
    owned_here,
)


def test_a_thread_this_device_opened_is_ours_under_either_name() -> None:
    """The daemon connection names one; the app-server we spawn names the other."""
    assert owned_here(DAEMON_CLIENT_NAME, "vscode") is True
    assert owned_here(EMBEDDED_CLIENT_NAME, "vscode") is True


@pytest.mark.parametrize(
    ("originator", "source"),
    [
        ("codex-tui", "cli"),
        ("codex_cli_rs", "cli"),
        ("codex_exec", "exec"),
        ("codex-tui", "vscode"),
    ],
)
def test_a_terminal_running_its_own_codex_is_ours(originator: str, source: str) -> None:
    assert owned_here(originator, source) is True


@pytest.mark.parametrize(
    "originator", ["Codex Desktop", "codex_work_desktop", "vscode-extension", "codex_cli_rs", None]
)
def test_another_application_on_this_machine_is_not_ours(originator: object) -> None:
    """An app-server source alone identifies neither a desktop app nor a terminal."""
    assert owned_here(originator, "vscode") is False


@pytest.mark.parametrize("source", [None, "", 7, ["cli"], {"subAgent": {"other": "guardian"}}])
def test_a_terminal_originator_does_not_admit_unreadable_or_subagent_sources(
    source: object,
) -> None:
    assert owned_here("codex-tui", source) is False


def test_a_terminal_originator_does_not_admit_an_unknown_source() -> None:
    assert owned_here("codex-tui", "unknown") is False


def test_a_subagent_is_not_a_session_whoever_spawned_it() -> None:
    """A subagent carries its parent's originator, so `source` has to decide alone."""
    spawn = {"subagent": {"thread_spawn": {"parent_thread_id": "01a08229", "depth": 1}}}
    assert owned_here("codex-tui", spawn) is False
    assert owned_here("Codex Desktop", {"subAgent": {"other": "guardian"}}) is False
    # A subagent of one of this device's own threads is still not a session.
    assert owned_here(DAEMON_CLIENT_NAME, spawn) is False
    assert owned_here(EMBEDDED_CLIENT_NAME, spawn) is False


def test_provenance_that_cannot_be_read_is_foreign() -> None:
    assert owned_here(None, None) is False
    assert owned_here("", "") is False
    assert owned_here(7, ["cli"]) is False
    # A name of ours says nothing on its own: where the client sits has to be
    # readable too, or the thread could be anything.
    assert owned_here(DAEMON_CLIENT_NAME, None) is False
    assert owned_here(DAEMON_CLIENT_NAME, "") is False
    # A name we do not connect under, wherever it claims to sit.
    assert owned_here("spike-A", "vscode") is False
    assert owned_here("Codex Desktop", "appServer") is False
