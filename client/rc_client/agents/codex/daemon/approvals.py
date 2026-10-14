"""Approval requests from the shared daemon, mapped onto protocol `approval` blocks.

The daemon tells us which decisions a particular prompt accepts in
`availableDecisions`, so the option list is built from that rather than
hardcoded, and every option carries the raw decision the daemon wants back.
Amendment A11 section 5.7 fixes the four ids and their labels.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

COMMAND_METHOD = "item/commandExecution/requestApproval"
FILE_CHANGE_METHOD = "item/fileChange/requestApproval"
PERMISSIONS_METHOD = "item/permissions/requestApproval"
APPROVAL_METHODS = frozenset({COMMAND_METHOD, FILE_CHANGE_METHOD, PERMISSIONS_METHOD})

ALLOW = "allow"
ALLOW_SESSION = "allow_session"
ALLOW_ALWAYS = "allow_always"
DENY = "deny"
# Never offered, only reported: the id a block resolves with when somebody else
# answered the daemon first.
ELSEWHERE = "elsewhere"

LABELS = {
    ALLOW: ("Allow", "primary"),
    ALLOW_SESSION: ("Allow for this session", "secondary"),
    ALLOW_ALWAYS: ("Always allow commands like this", "secondary"),
    DENY: ("Deny", "danger"),
}
# The order apps see, independent of the order the daemon lists its decisions in.
ORDER = (ALLOW, ALLOW_SESSION, ALLOW_ALWAYS, DENY)

# Codex decision names, as they travel in `availableDecisions` and in our reply.
ACCEPT = "accept"
ACCEPT_FOR_SESSION = "acceptForSession"
ACCEPT_WITH_AMENDMENT = "acceptWithExecpolicyAmendment"
DECLINE = "decline"
CANCEL = "cancel"

_BY_DECISION = {
    ACCEPT: ALLOW,
    ACCEPT_FOR_SESSION: ALLOW_SESSION,
    ACCEPT_WITH_AMENDMENT: ALLOW_ALWAYS,
    CANCEL: DENY,
    DECLINE: DENY,
}
# What a request offers when the daemon sends no `availableDecisions` at all.
FILE_CHANGE_DECISIONS: tuple[str, ...] = (ACCEPT, ACCEPT_FOR_SESSION, CANCEL)
PERMISSION_DECISIONS: tuple[str, ...] = (ACCEPT, ACCEPT_FOR_SESSION, DECLINE)


def decision_name(decision: Any) -> str:
    """The name of a decision that may be a bare string or a one-key object."""
    if isinstance(decision, str):
        return decision
    if isinstance(decision, dict) and len(decision) == 1:
        return str(next(iter(decision)))
    return ""


@dataclass(slots=True)
class ApprovalOptions:
    """The options one prompt offers, with the reply each of them stands for."""

    options: list[dict[str, str]] = field(default_factory=list)
    decisions: dict[str, Any] = field(default_factory=dict)

    def wire(self) -> list[dict[str, str]]:
        return [dict(option) for option in self.options]

    def offers(self, option_id: str) -> bool:
        return option_id in self.decisions


def build_options(available: Any, fallback: tuple[str, ...]) -> ApprovalOptions:
    """Turn `availableDecisions` into protocol options, keeping one per id.

    `cancel` beats `decline` when a prompt offers both, because A11 names
    `cancel` as the deny option and two "Deny" buttons would be nonsense.
    """
    raw = list(available) if isinstance(available, list) and available else list(fallback)
    names = {decision_name(item) for item in raw}
    chosen: dict[str, Any] = {}
    for item in raw:
        name = decision_name(item)
        option_id = _BY_DECISION.get(name)
        if option_id is None:
            continue
        if name == DECLINE and CANCEL in names:
            continue
        chosen.setdefault(option_id, item)
    if ALLOW not in chosen and ALLOW_SESSION in chosen:
        # Every set needs one primary option; promote the nearest thing to it.
        chosen[ALLOW] = chosen[ALLOW_SESSION]
    if DENY not in chosen:
        chosen[DENY] = CANCEL
    options = [
        {"id": option_id, "label": LABELS[option_id][0], "style": LABELS[option_id][1]}
        for option_id in ORDER
        if option_id in chosen
    ]
    return ApprovalOptions(options=options, decisions=chosen)


def command_reply(decision: Any) -> dict[str, Any]:
    """The result for a command or file-change approval request."""
    return {"decision": decision}


def permission_reply(option_id: str, params: dict[str, Any]) -> dict[str, Any]:
    """The result for a permissions request: a profile plus the grant's scope."""
    granted = params.get("permissions") if option_id != DENY else None
    return {
        "permissions": granted if isinstance(granted, dict) else {},
        "scope": "session" if option_id == ALLOW_SESSION else "turn",
    }
