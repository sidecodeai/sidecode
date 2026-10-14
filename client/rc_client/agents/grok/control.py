"""The A28 section 4.4 origin/control table for a Grok session on the leader."""

from __future__ import annotations


def resolve(
    created_here: bool,
    registered: bool,
    attached: bool,
    local_turn: bool = False,
) -> tuple[str, str]:
    """`(origin, control)` for one Grok session.

    `registered` is Grok's own session registry naming the session with a live
    process, which is the only signal there is that a TUI is in it: the leader
    says nothing when one exits. `attached` is the device holding the session in
    the leader, which is what makes a registered session `shared` rather than
    merely watched — a `grok` started with `use_leader` off, or under a sandbox
    profile, runs its own agent, so the leader never has its session and the
    mirror keeps it as `terminal`.

    Once the terminal is gone the session stays the device's to drive, because
    the leader still holds it: `remote` while a turn this device started is
    running, `none` when it is idle, and `none` is resumable in place. `origin`
    never changes.
    """
    origin = "remote" if created_here else "terminal"
    if registered:
        return origin, "shared" if attached else "terminal"
    if not attached:
        return origin, "none"
    if created_here or local_turn:
        return origin, "remote"
    return origin, "none"
