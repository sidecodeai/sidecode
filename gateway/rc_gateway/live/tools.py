"""Running the assistant's tools: one account's data in, a small JSON answer out.

A tool never raises to the model. A refusal or failure is returned as ``{"error": code}`` so the
assistant can tell the developer; what the tool did is logged by name and ids only. Every result
that carries something a coding agent wrote, a session's title included, is marked untrusted
(``digest.py``).
"""

from __future__ import annotations

import json
from typing import Any

from ..logging import logger
from .access import AccountAccess, OwnedSession
from .digest import (
    DECISIONS_PER_SESSION_MAX,
    PENDING_STATES,
    SESSIONS_LISTED_MAX,
    UNTRUSTED_NOTICE,
    WAITING_SESSIONS_MAX,
    clean,
    context_text,
    pending_decisions,
    session_row,
)
from .drafts import DraftBox
from .errors import LiveError
from .focus import Focus
from .gate import ConfirmGate
from .tool_specs import TOOLS

log = logger("rc_gateway.live.tools")

KNOWN_TOOLS = frozenset(str(tool["name"]) for tool in TOOLS)
#: The tool by which a spoken yes sends the draft.
SEND_TOOL = "confirm_send"
TITLE_CHARS = 80

#: What the model is told when a spoken confirmation is refused, by reason.
REFUSALS = {
    "not_read_back": (
        "The draft has not been read back to the developer in full, so nothing they said counts "
        "as a yes to it. Read the whole draft back, then wait for their answer. If this keeps "
        "failing, tell them they can press Send on screen."
    ),
    "not_confirmed": (
        "The developer has not answered the read-back yet: a yes from before the draft was read "
        "back does not count. Wait for their answer to the read-back. If they did say yes after "
        "it and this keeps failing, tell them to press Send on screen."
    ),
    "read_back_interrupted": (
        "The developer interrupted the read-back of this draft, so it can no longer be confirmed "
        "by voice. Call prepare_message again with what they now want, read the new draft back in "
        "full, and wait for their answer; or tell them they can press Send on screen."
    ),
}


class ToolRunner:
    def __init__(self, access: AccountAccess, drafts: DraftBox, gate: ConfirmGate) -> None:
        self._access = access
        self._drafts = drafts
        self._gate = gate
        #: The session page the browser says is on screen. It marks a row of ``list_sessions`` and
        #: does nothing else: no access decision looks at it.
        self.focus: Focus | None = None

    async def run(self, name: str, raw_arguments: str) -> str:
        # A name the model made up is its own content: it is logged as a name only when it is ours.
        logged = name if name in KNOWN_TOOLS else "unknown"
        try:
            arguments = json.loads(raw_arguments) if raw_arguments else {}
            if not isinstance(arguments, dict):
                raise LiveError("bad_arguments", "arguments must be an object")
            result = await self._dispatch(name, arguments)
        except LiveError as exc:
            log.info("live tool refused", tool=logged, refusal=exc.code, **self._tally(exc.code))
            result = {"error": exc.code, "message": str(exc)}
        except ValueError:
            result = {"error": "bad_arguments"}
        except Exception as exc:
            log.warning("live tool failed", tool=logged, error=type(exc).__name__)
            result = {"error": "tool_failed"}
        else:
            log.info("live tool ran", tool=logged)
        return json.dumps(result, ensure_ascii=False)

    def _tally(self, refusal: str) -> dict[str, float]:
        """For a refused spoken confirmation, the seconds of voice the gate had counted."""
        if refusal not in REFUSALS:
            return {}
        return {"voiced_seconds": round(self._gate.voiced_seconds, 2)}

    async def _dispatch(self, name: str, args: dict[str, Any]) -> Any:
        if name == "list_sessions":
            return await self._list_sessions()
        if name == "get_session_status":
            return await self._status(_text(args, "session"))
        if name == "list_pending_decisions":
            return await self._pending()
        if name == "get_session_context":
            return await self._context(_text(args, "session"))
        if name == "prepare_message":
            return await self._prepare(_text(args, "session"), _text(args, "text"))
        if name == SEND_TOOL:
            return await self.send_draft(_text(args, "draft_id"), spoken=True)
        raise LiveError("unknown_tool", "no such tool")

    async def _list_sessions(self) -> dict[str, Any]:
        rows = [self._listed(item) for item in await self._access.sessions()]
        # The session on screen comes first, so the cap on the list cannot cut it.
        rows.sort(key=lambda row: "on_screen" not in row)
        return {
            "notice": UNTRUSTED_NOTICE,
            "sessions": rows[:SESSIONS_LISTED_MAX],
            "total": len(rows),
        }

    def _listed(self, item: OwnedSession) -> dict[str, Any]:
        """One row of the list, marked when it is the session page the browser has open."""
        row = session_row(item)
        if self.focus is not None and self.focus.matches(item):
            row["on_screen"] = True
        return row

    async def _status(self, ref: str) -> dict[str, Any]:
        return {"notice": UNTRUSTED_NOTICE, **session_row(await self._access.resolve(ref))}

    async def _pending(self) -> dict[str, Any]:
        waiting: list[dict[str, Any]] = []
        for item in await self._access.sessions():
            if item.get("state") not in PENDING_STATES:
                continue
            row = session_row(item)
            decisions = pending_decisions(self._access.tail(item))
            waiting.append(
                {
                    "session": row["title"],
                    "session_id": row["id"],
                    "project": row["project"],
                    "state": row["state"],
                    "detail": row["detail"],
                    "decisions": decisions[:DECISIONS_PER_SESSION_MAX],
                }
            )
        return {"notice": UNTRUSTED_NOTICE, "waiting": waiting[:WAITING_SESSIONS_MAX]}

    async def _context(self, ref: str) -> dict[str, Any]:
        item = await self._access.resolve(ref)
        return {
            "notice": UNTRUSTED_NOTICE,
            "session": session_row(item),
            "untrusted_transcript": context_text(self._access.tail(item)),
        }

    async def _prepare(self, ref: str, text: str) -> dict[str, Any]:
        item = await self._access.resolve(ref)
        title = clean(item.get("title") or "", TITLE_CHARS)
        draft = self._drafts.prepare(str(item.get("session_id")), title, text)
        return {
            "notice": UNTRUSTED_NOTICE,
            "draft_id": draft.draft_id,
            "session": title,
            "text": draft.text,
            "next": "Read this back, then wait for an explicit yes before confirm_send.",
        }

    async def send_draft(self, draft_id: str, *, spoken: bool) -> dict[str, Any]:
        """Where a drafted message leaves: a spoken yes must pass the gate, a button need not."""
        pending = self._drafts.pending()
        if pending is None or pending.draft_id != draft_id:
            raise LiveError("no_such_draft", "that message is no longer waiting")
        if spoken and (reason := self._gate.refusal(draft_id)) is not None:
            raise LiveError(reason, REFUSALS[reason])
        draft = self._drafts.take(draft_id)
        try:
            accepted = await self._access.send(draft.session_id, draft.text)
        except LiveError:
            self._drafts.restore(draft)
            raise
        self._gate.forget()
        self._drafts.sent += 1
        log.info("live message sent", session_id=draft.session_id, accepted=accepted)
        return {
            "notice": UNTRUSTED_NOTICE,
            "sent": True,
            "accepted": accepted,
            "session": draft.session_title,
            "draft_id": draft.draft_id,
        }


def _text(args: dict[str, Any], name: str) -> str:
    value = args.get(name)
    if not isinstance(value, str):
        raise LiveError("bad_arguments", f"{name} must be text")
    return value


def sent_draft(output: str) -> tuple[str, str] | None:
    """The draft id and session label in a tool's answer that says a draft went, else ``None``."""
    try:
        answer = json.loads(output)
    except ValueError:
        return None
    if not isinstance(answer, dict) or answer.get("sent") is not True:
        return None
    draft_id, session = answer.get("draft_id"), answer.get("session")
    if isinstance(draft_id, str) and isinstance(session, str):
        return draft_id, session
    return None
