"""Which Codex threads on this machine are the device's to publish (A18, A48, A59).

Codex keeps one history for the whole machine, so the desktop app's chats and
scheduled automations, an IDE extension's threads and the subagents a thread
spawned all sit in the daemon's index beside what somebody typed at a terminal.
Every thread records the client that created it — `originator` on the index
entry and in the rollout's own `session_meta` — and where that client sits:
`source` is `"cli"` for a standalone TUI, `"exec"` for `codex exec`, `"vscode"`
for an app-server client, and an object for a subagent. A terminal TUI using
an app-server also reports `"vscode"`, with `originator: "codex-tui"` (A48).
"""

from __future__ import annotations

# The device's stable client identity when it connects to the shared daemon.
DAEMON_CLIENT_NAME = "remote-control"

# The name the app-server the device spawns for itself announces, which is the
# fallback path when there is no daemon to join.
EMBEDDED_CLIENT_NAME = "rc-client"

OWN_ORIGINATORS = frozenset({DAEMON_CLIENT_NAME, EMBEDDED_CLIENT_NAME})

# The `source` of a terminal on this machine running its own Codex.
TERMINAL_SOURCE = "cli"

# What a `codex exec` run records about itself: a script's or an automation's
# work, which nobody sits in and nobody continues (A59).
EXEC_ORIGINATOR = "codex_exec"
EXEC_SOURCE = "exec"


def owned_here(originator: object, source: object) -> bool:
    """Whether a Codex thread is this device's to publish.

    Two kinds are: one the device itself opened, whichever name it connected
    under, and one a terminal started on its own Codex. Both have to sit
    somewhere a person can reach, which is what `source` being a plain string
    says: a `source` object is a thread another thread spawned for itself, and
    a subagent is never a session whoever its parent belonged to. A `codex
    exec` run is never one either (A59). Everything else belongs to the
    application that started it, and so does anything whose provenance cannot
    be read.
    """
    if not isinstance(source, str) or not source:
        return False
    if originator == EXEC_ORIGINATOR or source == EXEC_SOURCE:
        return False
    if isinstance(originator, str) and originator in OWN_ORIGINATORS:
        return True
    return source == TERMINAL_SOURCE or (originator == "codex-tui" and source == "vscode")
