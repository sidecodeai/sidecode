"""A fake `agent agent stdio` peer: real ACP framing, recorded replies.

The runner spawns this through a shim exactly as it spawns Grok, so the
subprocess, the JSON-RPC framing and the reader task are all exercised. What it
answers comes from `tests/fixtures/grok/`, recorded from the real agent.

`RC_FAKE_GROK_DIR` names a scratch directory: `mode` picks the scenario and
every message the peer receives is appended to `requests.jsonl`.

Modes beginning `question` ask the person something mid-turn the way Grok does
(A52): `_x.ai/ask_user_question` after the recorded rows that announce it, and the
tool's result built from whatever the client replies; `question-timeout` gives
up by itself, as Grok's own deadline does, without waiting for a reply.

`late-end` answers its first prompt before that turn's own `turn_completed`, and
sends the held row only once the second prompt has asked its question: the late
ending of a turn that is over, which must not touch the next one. `late-fail`
does the same with a first prompt that fails half-way, so the device ends that
turn itself; `leader-stuck` never settles a cancelled prompt.

Modes beginning `leader` stand in for the machine's leader (A28): several
sessions on one peer, `_x.ai/session/info` answering `{}` for a session nobody
loaded, a `session/load` that replays with `isReplay` and its own `eventId`s,
and every prompt echoed back the way a leader echoes one to every client. A test
pushes anything the leader would send unasked — a permission request, a prompt
somebody typed at the TUI, `interaction_resolved`, `_x.ai/sessions/changed` — by
dropping the whole JSON-RPC message into the peer's `inject/` directory.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "grok"
SESSION_ID = "01a09b4f-1b25-7e92-b96b-356d292ff2d0"
INJECT_INTERVAL = 0.02
QUESTION_ID = 9002
# The prompt ids the recorded turns carry. Every prompt the peer answers gets one
# of its own in their place, as every prompt Grok runs does.
RECORDED_PROMPTS = (
    "a7fdbcb6-2e2b-4bec-83e2-fbee92ee91e6",
    "69a928fd-27bf-42c1-9e55-01f9d43016ec",
    "467e217d-f6ea-43d0-baf7-4b1d57b3426d",
)
# The tool's own words, as Grok 1.0.62 wrote them (`fixtures/grok/question-endings.json`).
ANSWERED = "User has answered your questions: "
CONTINUE = ". You can now continue with the user's answers in mind."
DECLINED = (
    "User declined to answer the questions. Continue with the task using your best "
    "judgment, or ask different questions."
)
# How long `question-timeout` waits before giving up on the question by itself.
QUESTION_DEADLINE = 0.3
# The modes whose prompt fails, with the JSON-RPC error each answers it with.
FAILURES = {
    "refuse": (-32602, "Invalid prompt"),
    "fail-midway": (-32602, "Model overloaded"),
    "break": (-32603, "Internal error"),
}


def _handshake(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    return data


def _notifications(name: str) -> list[dict[str, Any]]:
    rows = []
    for line in (FIXTURES / name).read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


class FakePeer:
    def __init__(self) -> None:
        self.directory = Path(os.environ["RC_FAKE_GROK_DIR"])
        self.mode = (self.directory / "mode").read_text(encoding="utf-8").strip()
        self.log = (self.directory / "requests.jsonl").open("a", encoding="utf-8")
        self.cancelled = False
        self.prompt_id: Any = None
        self.prompt_session = SESSION_ID
        self.lock = threading.Lock()
        self.settings: dict[str, Any] = _settings(self.directory)
        # The question this turn has open, if any, and what it asked.
        self.question: dict[str, Any] | None = None
        self.turns = 0
        self.turn_id = ""
        # `late-end`: the first turn's own `turn_completed`, sent only once the
        # next turn has asked its question.
        self.held: dict[str, Any] | None = None

    @property
    def leader(self) -> bool:
        return self.mode.startswith("leader")

    # ------------------------------------------------------------------ wire

    def send(self, payload: dict[str, Any]) -> None:
        with self.lock:
            sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
            sys.stdout.flush()

    def result(self, message_id: Any, result: dict[str, Any]) -> None:
        self.send({"jsonrpc": "2.0", "id": message_id, "result": result})

    def notify(self, method: str, params: dict[str, Any]) -> None:
        self.send({"jsonrpc": "2.0", "method": method, "params": params})

    def record(self, message: dict[str, Any]) -> None:
        self.log.write(json.dumps(message, ensure_ascii=False) + "\n")
        self.log.flush()

    # ------------------------------------------------------------- scenarios

    def run(self) -> None:
        if self.leader:
            threading.Thread(target=self.inject_loop, daemon=True).start()
        for line in sys.stdin:
            if not line.strip():
                continue
            message = json.loads(line)
            self.record(message)
            self.dispatch(message)

    def inject_loop(self) -> None:
        """Forward whatever a test drops into `inject/`, in name order, once each."""
        inject = self.directory / "inject"
        sent: set[str] = set()
        while True:
            for path in sorted(inject.glob("*.json")) if inject.is_dir() else []:
                if path.name in sent:
                    continue
                try:
                    payload = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                sent.add(path.name)
                self.send(payload)
            time.sleep(INJECT_INTERVAL)

    def dispatch(self, message: dict[str, Any]) -> None:
        method = str(message.get("method") or "")
        message_id = message.get("id")
        if method == "session/cancel":
            if self.mode != "leader-stuck":
                self.cancel()
            return
        if not method:
            if message_id == QUESTION_ID and self.question is not None:
                self.answered(message)
                return
            # The client answered a permission request, so the turn can finish.
            self.finish_prompt()
            return
        if message_id is None:
            return
        handshake = _handshake("handshake.json")
        params = message.get("params") or {}
        if method == "initialize":
            self.result(message_id, self.initialize(handshake["initialize"]))
        elif method == "_x.ai/session/info":
            self.result(message_id, self.session_info(params))
        elif method == "_x.ai/session/list":
            self.result(message_id, {"sessions": self.listed()})
        elif method == "session/list":
            self.result(message_id, {"sessions": [{"sessionId": self.loaded_id()}]})
        elif method == "session/close":
            # Sent only by the close of a session no terminal is in (A39); for a
            # session a terminal registered it is a real fault (A28).
            self.result(message_id, {})
        elif method == "session/new":
            self.result(message_id, self.new_session(handshake["session/new"]))
            self.advertise_commands()
        elif method == "session/load":
            self.load(message_id, params)
            self.advertise_commands()
        elif method == "session/set_mode":
            self.result(message_id, {})
        elif method == "session/set_config_option":
            self.set_option(message_id, message.get("params") or {})
        elif method == "session/prompt":
            self.prompt(message_id, params)
        else:
            self.send(
                {
                    "jsonrpc": "2.0",
                    "id": message_id,
                    "error": {"code": -32601, "message": "Method not found"},
                }
            )

    def advertise_commands(self) -> None:
        """Grok pushes its whole command list unasked once a session is open."""
        self.send(_handshake("available-commands.json"))

    def initialize(self, recorded: dict[str, Any]) -> dict[str, Any]:
        """The leader reports its own version, which is the drift signal (A28)."""
        if not self.leader:
            return recorded
        meta = dict(recorded.get("_meta") or {})
        meta["agentVersion"] = str(self.settings.get("agentVersion") or "1.0.30")
        return {**recorded, "_meta": meta}

    def loaded(self) -> list[str]:
        """Which sessions the leader is holding; an empty list means none."""
        if "loaded" not in self.settings:
            return [SESSION_ID]
        return [str(item) for item in self.settings["loaded"] or []]

    def loaded_id(self) -> str:
        found = self.loaded()
        return found[0] if found else SESSION_ID

    def session_info(self, params: dict[str, Any]) -> dict[str, Any]:
        """Rich for a session the leader holds, `{}` for one it does not."""
        session_id = str(params.get("sessionId") or "")
        if session_id not in set(self.loaded()):
            return {}
        return {"sessionId": session_id, "turns": 2, "context": {"used": 1024}}

    def listed(self) -> list[dict[str, Any]]:
        titles: dict[str, Any] = self.settings.get("titles") or {}
        return [{"sessionId": session_id, "title": title} for session_id, title in titles.items()]

    def new_session(self, recorded: dict[str, Any]) -> dict[str, Any]:
        chosen = self.settings.get("newSessionId")
        if not chosen:
            return recorded
        self.prompt_session = str(chosen)
        return {**recorded, "sessionId": str(chosen)}

    def load(self, message_id: Any, params: dict[str, Any]) -> None:
        resumed = _handshake("resume-handshake.json")
        session_id = str(params.get("sessionId") or SESSION_ID)
        self.prompt_session = session_id
        for row in _notifications("resume.jsonl"):
            meta = (row.get("params") or {}).get("_meta") or {}
            if meta.get("isReplay"):
                self.send(_for_session(row, session_id))
        self.result(message_id, resumed["session/load"])

    def set_option(self, message_id: Any, params: dict[str, Any]) -> None:
        options: list[dict[str, Any]] = [
            {"id": "model", "name": "Model", "category": "model", "type": "select"},
            {
                "id": "reasoning_effort",
                "name": "Reasoning Effort",
                "category": "thought_level",
                "type": "select",
            },
        ]
        for option in options:
            current = params.get("value") if option["id"] == params.get("configId") else None
            option["currentValue"] = current or ("grok-4.6" if option["id"] == "model" else "high")
        self.result(message_id, {"configOptions": options})

    def prompt(self, message_id: Any, params: dict[str, Any]) -> None:
        self.prompt_id = message_id
        self.turns += 1
        self.turn_id = f"prompt-{self.turns}"
        if params.get("sessionId"):
            self.prompt_session = str(params["sessionId"])
        if self.leader:
            # A leader echoes every prompt to every client, this one included.
            self.echo(_text_of(params.get("prompt")))
        if self.mode == "approval":
            self.ask_permission()
            return
        if self.mode.startswith("question"):
            self.ask_question()
            return
        if self.mode in {"late-end", "late-fail"}:
            if self.turns == 1 and self.mode == "late-end":
                self.answer_first()
            elif self.turns == 1:
                self.fail_holding_end(message_id)
            else:
                self.ask_question()
                if self.held is not None:
                    self.send(self.held)
            return
        if self.mode == "stray-reply":
            # Grok answers requests of its own on the same stream (1.0.62, in a
            # fresh home's first turn); no client ever sent this one.
            self.result("skills-reload", {"result": {"reloaded": 1}})
        if self.mode in FAILURES:
            # Refused as it stood, failed once the turn had begun to stream, or
            # failed inside Grok before it said anything.
            if self.mode == "fail-midway":
                for row in _notifications("turn.jsonl")[:4]:
                    self.send(self.this_turn(row))
            code, message = FAILURES[self.mode]
            self.send(
                {"jsonrpc": "2.0", "id": message_id, "error": {"code": code, "message": message}}
            )
            self.prompt_id = None
            return
        if self.mode in {"cancel", "leader-cancel", "leader-stuck"}:
            for row in _notifications("turn.jsonl")[:4]:
                self.send(self.this_turn(row))
            return
        self.finish_prompt()

    def echo(self, text: str) -> None:
        self.send(
            {
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {
                    "sessionId": self.prompt_session,
                    "update": {
                        "sessionUpdate": "user_message_chunk",
                        "content": {"type": "text", "text": text},
                    },
                    "_meta": {"eventId": f"{self.prompt_session}-900", "promptId": "p-900"},
                },
            }
        )

    def finish_prompt(self) -> None:
        if self.prompt_id is None:
            return
        # A slash command runs inside Grok itself: one message, no tools, no usage.
        source = "command.jsonl" if self.mode == "command" else "turn.jsonl"
        for row in _notifications(source):
            self.send(self.this_turn(row))
        self.result(self.prompt_id, {"stopReason": "end_turn", "_meta": {"promptId": self.turn_id}})
        self.prompt_id = None

    def answer_first(self) -> None:
        """The prompt's answer ahead of its own `turn_completed`, which is held back."""
        for row in _notifications("turn.jsonl"):
            row = self.this_turn(row)
            update = (row.get("params") or {}).get("update") or {}
            if update.get("sessionUpdate") == "turn_completed":
                self.held = row
                continue
            self.send(row)
        self.result(self.prompt_id, {"stopReason": "end_turn", "_meta": {"promptId": self.turn_id}})
        self.prompt_id = None

    def fail_holding_end(self, message_id: Any) -> None:
        """Fail half-way, as `fail-midway` does, holding the turn's own `turn_completed` back."""
        rows = [self.this_turn(row) for row in _notifications("turn.jsonl")]
        for row in rows[:4]:
            self.send(row)
        self.held = next(
            row
            for row in rows
            if ((row.get("params") or {}).get("update") or {}).get("sessionUpdate")
            == "turn_completed"
        )
        code, message = FAILURES["fail-midway"]
        self.send({"jsonrpc": "2.0", "id": message_id, "error": {"code": code, "message": message}})
        self.prompt_id = None

    def this_turn(self, row: dict[str, Any]) -> dict[str, Any]:
        """A recorded row of the turn under way: its session, and its own prompt id."""
        text = json.dumps(_for_session(row, self.prompt_session))
        for recorded in RECORDED_PROMPTS:
            text = text.replace(recorded, self.turn_id)
        moved: dict[str, Any] = json.loads(text)
        return moved

    def ask_permission(self) -> None:
        self.send(
            {
                "jsonrpc": "2.0",
                "id": 9001,
                "method": "session/request_permission",
                "params": {
                    "sessionId": SESSION_ID,
                    "toolCall": {
                        "toolCallId": "call-1",
                        "title": "run_terminal_command",
                        "kind": "execute",
                        "rawInput": {"command": "ls -la"},
                    },
                    "options": [
                        {"optionId": "allow-once", "name": "Allow once", "kind": "allow_once"},
                        {
                            "optionId": "allow-always",
                            "name": "Always allow",
                            "kind": "allow_always",
                        },
                        {"optionId": "reject-once", "name": "Reject", "kind": "reject_once"},
                    ],
                },
            }
        )

    def ask_question(self) -> None:
        """The recorded rows up to and including `_x.ai/ask_user_question` (A52)."""
        for row in _notifications("question.jsonl"):
            row = self.this_turn(row)
            if row.get("method") == "_x.ai/ask_user_question":
                self.question = dict(row.get("params") or {})
            self.send(row)
        if self.mode == "question-timeout":
            threading.Timer(QUESTION_DEADLINE, self.give_up).start()

    def give_up(self) -> None:
        """Grok's own deadline: nobody answered, so nobody is waited for any more."""
        if self.question is None:
            return
        self.end_question({"type": "AskUserQuestion", "UserAnswered": {"message": DECLINED}})
        self.finish_prompt()

    def answered(self, message: dict[str, Any]) -> None:
        """The client replied to the question: the tool's result says what it got."""
        result = message.get("result")
        if "error" in message:
            reason = str((message.get("error") or {}).get("message") or "")
            text = f"Failed to reach the client for user question: {reason}"
            self.end_question({"error": "tool_execution_failed", "message": text}, "failed")
            self.finish_prompt()
            return
        if not isinstance(result, dict) or result.get("outcome") != "accepted":
            # Declined: the model carries on, and only a cancel ends this turn.
            self.end_question({"type": "AskUserQuestion", "UserAnswered": {"message": DECLINED}})
            return
        pairs = ", ".join(
            f'"{question}"="{", ".join(answer) if isinstance(answer, list) else answer}"'
            for question, answer in (result.get("answers") or {}).items()
        )
        message_text = f"{ANSWERED}{pairs}{CONTINUE}"
        self.end_question({"type": "AskUserQuestion", "UserAnswered": {"message": message_text}})
        self.finish_prompt()

    def end_question(self, raw: dict[str, Any], status: str = "completed") -> None:
        """`interaction_resolved`, then the tool's result, as Grok sends them."""
        question = self.question
        if question is None:
            return
        self.question = None
        call = str(question.get("toolCallId") or "")
        self.notify(
            "_x.ai/session_notification",
            {
                "sessionId": self.prompt_session,
                "update": {"sessionUpdate": "interaction_resolved", "tool_call_id": call},
            },
        )
        text = str(raw.get("message") or (raw.get("UserAnswered") or {}).get("message") or "")
        self.notify(
            "session/update",
            {
                "sessionId": self.prompt_session,
                "update": {
                    "sessionUpdate": "tool_call_update",
                    "toolCallId": call,
                    "status": status,
                    "content": [{"type": "content", "content": {"type": "text", "text": text}}],
                    "rawOutput": raw,
                },
            },
        )

    def cancel(self) -> None:
        self.cancelled = True
        self.notify(
            "_x.ai/session_notification",
            {
                "sessionId": self.prompt_session,
                "update": {
                    "sessionUpdate": "turn_completed",
                    "stop_reason": "cancelled",
                    "elapsed_ms": 120,
                    "usage": {"inputTokens": 10, "outputTokens": 2, "totalTokens": 12},
                },
                "_meta": {"eventId": f"{self.prompt_session}-99"},
            },
        )
        if self.prompt_id is not None:
            self.result(
                self.prompt_id, {"stopReason": "cancelled", "_meta": {"promptId": self.turn_id}}
            )
            self.prompt_id = None


def _settings(directory: Path) -> dict[str, Any]:
    """Per-run leader settings: which sessions are loaded, their titles, its version."""
    path = directory / "leader.json"
    if not path.exists():
        return {}
    data: Any = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


def _for_session(row: dict[str, Any], session_id: str) -> dict[str, Any]:
    """A recorded row re-addressed to the session a test asked about."""
    if session_id == SESSION_ID:
        return row
    moved: dict[str, Any] = json.loads(json.dumps(row).replace(SESSION_ID, session_id))
    return moved


def _text_of(prompt: Any) -> str:
    if not isinstance(prompt, list):
        return ""
    return "".join(str(item.get("text") or "") for item in prompt if isinstance(item, dict))


def main() -> int:
    peer = FakePeer()
    try:
        peer.run()
    except (BrokenPipeError, KeyboardInterrupt):
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
