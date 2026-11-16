"""The A28 section 4.4 origin/control table for a Grok session on the leader."""

from __future__ import annotations


def resolve(
    created_here: bool,
    registered: bool,
    attached: bool,
    local_turn: bool = False,
    held: bool = False,
    closed: bool = False,
) -> tuple[str, str]:
    """`(origin, control)` for one Grok session.

    `registered` is Grok's own session registry naming the session with a live
    process, which is the only signal there is that a TUI is in it: the leader
    says nothing when one exits. `attached` is the device holding the session in
    the leader, which is what makes a registered session `shared` rather than
    merely watched — a `grok` started with `use_leader` off, or under a sandbox
    profile, runs its own agent, so the leader never has its session and the
    mirror keeps it as `terminal`.

    A session the device created, or one an app's message took (`held`), is the
    device's until the person closes it (A58): once the terminal is gone it is
    `remote`, joined or not, and the next message loads it. Any other is
    `remote` only while a turn this device started is running, and `none`
    otherwise; so is a `closed` one. `origin` never changes.
    """
    origin = "remote" if created_here else "terminal"
    if registered:
        return origin, "shared" if attached else "terminal"
    if (created_here or held) and not closed:
        return origin, "remote"
    if attached and local_turn:
        return origin, "remote"
    return origin, "none"
