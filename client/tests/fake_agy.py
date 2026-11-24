"""A fake `agy`: its `acp` server with real framing, its `--version` and its usage query.

The adapter starts this through a shim named `agy`, exactly as it starts the real one, so the
subprocess, the JSON-RPC framing and the reader are exercised end to end. Every answer is shaped
after the frames `agy` 1.5.13 and 1.5.14 write to a real client: the same methods, fields, string
request ids, option ids and error words.

`RC_FAKE_AGY_DIR` names a scratch directory. `config.json` there says what this `agy` is — its
version, whether anybody is signed in, its conversations, what a `session/load` replays, the usage
query's answer — and `turns` lists, in order, what each prompt does. Everything the fake receives
is appended to `requests.jsonl`, and each start's argv and directory to `starts.jsonl`.
"""

from __future__ import annotations

import json
import os
import queue
import sys
import threading
import time
from pathlib import Path
from typing import Any

SESSION_ID = "d055434c-60ae-474a-b411-363f558a64c0"
MODELS = [
    ("gemini-4-neon", "Gemini 4 Neon", ("low", "medium", "high", "xhigh"), "high"),
    ("gemini-4-carbon", "Gemini 4 Carbon", ("low", "medium", "high"), "high"),
    ("gemini-3.8-flash", "Gemini 3.8 Flash", ("low", "medium", "high"), "medium"),
    ("gpt-oss-120b-medium", "GPT-OSS 120B (Medium)", (), None),
]
LEVEL_NAMES = {"low": "Low", "medium": "Medium", "high": "High", "xhigh": "Extra"}
MODES = [
    ("default", "Default", "Ask before edits and commands, per your permission settings."),
    ("accept-edits", "Accept edits", "Approve file edits in the workspace without asking."),
    ("auto", "Auto", "Approve edits and safe commands; ask before irreversible actions."),
    ("plan", "Plan", "Plan carefully before executing a task."),
]
COMMANDS = [
    {"name": "goal", "description": "Run until the specified goal is completely finished."},
    {"name": "compact", "description": "Summarize the conversation to free up context."},
    {"name": "schedule", "description": "Schedule a prompt to run later."},
]
WINDOW = 1048576
STEER_REFUSED = (
    "Can't add this prompt to session {sid}: the running prompt is waiting for a permission or "
    "question answer; answer it or send session/cancel, then send this prompt again"
)
USAGE = {
    "conversation_id": "",
    "status": "SUCCESS",
    "command": {
        "name": "usage",
        "data": {
            "groups": [
                {
                    "name": "Gemini Models",
                    "description": "Models within this group: Gemini 4 Neon, Gemini 4 Carbon, "
                    "Gemini 3.8 Flash",
                    "buckets": [
                        {
                            "id": "gemini-weekly",
                            "window": "weekly",
                            "remaining_fraction": 0.875,
                            "reset_time": "2026-09-16T17:00:00Z",
                        },
                        {
                            "id": "gemini-5h",
                            "window": "5h",
                            "remaining_fraction": 0.75,
                            "reset_time": "2026-09-11T23:00:00Z",
                        },
                    ],
                },
                {
                    "name": "Claude and GPT models",
                    "description": "Models within this group: GPT-OSS 120B",
                    "buckets": [
                        {"id": "3p-weekly", "window": "weekly", "remaining_fraction": 1},
                        {"id": "3p-5h", "window": "5h", "remaining_fraction": 1},
                    ],
                },
            ]
        },
    },
}


class Fake:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.out_lock = threading.Lock()
        self.log_lock = threading.Lock()
        self.replies: dict[str, queue.Queue[dict[str, Any]]] = {}
        self.next_request = 0
        self.cancel = threading.Event()
        self.turn_index = self._counter("turn")
        self.sessions: dict[str, dict[str, Any]] = {}
        self.prompts: list[tuple[Any, str]] = []
        self.prompt_waiters: queue.Queue[tuple[Any, dict[str, Any]]] = queue.Queue()
        self.busy = False
        self.awaiting_answer = False
        self.titled: set[str] = set()

    # ---------------------------------------------------------------- plumbing

    @property
    def config(self) -> dict[str, Any]:
        """What the test says this agy is, read afresh: a test may change it between requests."""
        config: dict[str, Any] = json.loads((self.directory / "config.json").read_text())
        return config

    def _counter(self, name: str) -> int:
        """How many prompts earlier processes of this fake already ran, so turns go on in order."""
        path = self.directory / f"{name}.count"
        return int(path.read_text()) if path.exists() else 0

    def _count(self, name: str, value: int) -> None:
        (self.directory / f"{name}.count").write_text(str(value))

    def log(self, message: dict[str, Any]) -> None:
        with self.log_lock, (self.directory / "requests.jsonl").open("a") as handle:
            handle.write(json.dumps(message) + "\n")

    def send(self, payload: dict[str, Any]) -> None:
        with self.out_lock:
            sys.stdout.write(json.dumps(payload) + "\n")
            sys.stdout.flush()

    def result(self, request_id: Any, result: dict[str, Any]) -> None:
        self.send({"jsonrpc": "2.0", "id": request_id, "result": result})

    def error(self, request_id: Any, code: int, message: str) -> None:
        self.send({"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}})

    def update(self, sid: str, update: dict[str, Any]) -> None:
        self.send(
            {
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {"sessionId": sid, "update": update},
            }
        )

    def ask(self, method: str, params: dict[str, Any], timeout: float = 30.0) -> dict[str, Any]:
        """A request of agy's own, under a string id, and the client's answer to it."""
        self.next_request += 1
        request_id = str(self.next_request)
        waiting: queue.Queue[dict[str, Any]] = queue.Queue()
        self.replies[request_id] = waiting
        self.send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.cancel.is_set():
                return {"cancelled": True}
            try:
                return waiting.get(timeout=0.02)
            except queue.Empty:
                continue
        return {"timeout": True}

    # ----------------------------------------------------------------- options

    def options(self, sid: str) -> list[dict[str, Any]]:
        state = self.sessions[sid]
        model = next(item for item in MODELS if item[0] == state["model"])
        found: list[dict[str, Any]] = [
            {
                "id": "model",
                "name": "Model",
                "category": "model",
                "type": "select",
                "currentValue": state["model"],
                "options": [{"value": item[0], "name": item[1]} for item in MODELS],
            }
        ]
        if model[2]:
            found.append(
                {
                    "id": "thought_level",
                    "name": "Thinking Level",
                    "category": "thought_level",
                    "type": "select",
                    "currentValue": state["level"],
                    "options": [{"value": lv, "name": LEVEL_NAMES[lv]} for lv in model[2]],
                }
            )
        found.append(
            {
                "id": "mode",
                "name": "Mode",
                "category": "mode",
                "type": "select",
                "currentValue": state["mode"],
                "options": [
                    {"value": mid, "name": name, "description": text} for mid, name, text in MODES
                ],
            }
        )
        return found

    def opened(self, sid: str) -> dict[str, Any]:
        """A session on the account's defaults, as every new process starts one."""
        self.sessions[sid] = {"model": "gemini-4-neon", "level": "high", "mode": "default"}
        return {
            "sessionId": sid,
            "modes": {
                "currentModeId": "default",
                "availableModes": [
                    {"id": mid, "name": name, "description": text} for mid, name, text in MODES
                ],
            },
            "configOptions": self.options(sid),
            "_meta": {"antigravity": {"tier": {"name": "Antigravity Starter Quota"}}},
        }

    # ---------------------------------------------------------------- requests

    def handle(self, message: dict[str, Any]) -> None:
        method = message.get("method")
        request_id = message.get("id")
        if method is None:
            waiting = self.replies.pop(str(request_id), None)
            if waiting is not None:
                waiting.put(message.get("result") or {"error": message.get("error")})
            return
        params = message.get("params") or {}
        handler = getattr(self, "on_" + str(method).replace("/", "_"), None)
        if handler is None:
            if request_id is not None:
                self.error(request_id, -32601, f"Method not found: {method}")
            return
        handler(request_id, params)

    def signed_in(self, request_id: Any) -> bool:
        if self.config.get("signed_out"):
            self.error(request_id, -32000, "Authentication required")
            return False
        return True

    def on_initialize(self, request_id: Any, params: dict[str, Any]) -> None:
        self.result(
            request_id,
            {
                "protocolVersion": 1,
                "agentCapabilities": {
                    "loadSession": True,
                    "promptCapabilities": {"image": True, "audio": False, "embeddedContext": True},
                    "sessionCapabilities": {"resume": {}, "close": {}, "load": {}, "list": {}},
                },
                "agentInfo": {
                    "name": "Antigravity ACP Server",
                    "version": self.config.get("version", "1.5.14"),
                },
                "authMethods": [{"id": "oauth-personal", "name": "Log in with Google"}],
            },
        )

    def on_session_new(self, request_id: Any, params: dict[str, Any]) -> None:
        if not self.signed_in(request_id):
            return
        if self.config.get("backend_fails"):
            self.error(
                request_id,
                -32603,
                "Failed to start Antigravity: language server failed to start: "
                "listen tcp 127.0.0.1:0: bind: operation not permitted",
            )
            return
        sid = str(self.config.get("session_id", SESSION_ID))
        self.result(request_id, self.opened(sid))
        self.update(
            sid, {"sessionUpdate": "available_commands_update", "availableCommands": COMMANDS}
        )
        self.update(sid, {"sessionUpdate": "usage_update", "used": 0, "size": WINDOW})

    def listed(self) -> list[dict[str, Any]]:
        return list(self.config.get("list", []))

    def on_session_list(self, request_id: Any, params: dict[str, Any]) -> None:
        if self.signed_in(request_id):
            self.result(request_id, {"sessions": self.listed()})

    def on_session_resume(self, request_id: Any, params: dict[str, Any]) -> None:
        if not self.signed_in(request_id):
            return
        sid = str(params.get("sessionId"))
        if sid not in {item.get("sessionId") for item in self.listed()}:
            self.error(
                request_id,
                -32603,
                f"Failed to read session {sid}; retry, or load it with session/load",
            )
            return
        self.result(request_id, self.opened(sid))
        self.update(
            sid, {"sessionUpdate": "available_commands_update", "availableCommands": COMMANDS}
        )
        self.titled.add(sid)
        self.update(sid, {"sessionUpdate": "session_info_update", "title": "Listed Title"})

    def on_session_load(self, request_id: Any, params: dict[str, Any]) -> None:
        if not self.signed_in(request_id):
            return
        sid = str(params.get("sessionId"))
        for update in self.config.get("replay", {}).get(sid, []):
            self.update(sid, update)
        self.result(request_id, self.opened(sid))
        self.update(sid, {"sessionUpdate": "usage_update", "used": 14823, "size": WINDOW})
        self.update(
            sid, {"sessionUpdate": "available_commands_update", "availableCommands": COMMANDS}
        )
        self.titled.add(sid)
        self.update(sid, {"sessionUpdate": "session_info_update", "title": "Listed Title"})

    def on_session_set_config_option(self, request_id: Any, params: dict[str, Any]) -> None:
        sid = str(params.get("sessionId"))
        state = self.sessions.get(sid)
        if state is None:
            self.error(request_id, -32602, f"Session not found: {sid}")
            return
        option, value = params.get("configId"), params.get("value")
        if option == "model":
            model = next((item for item in MODELS if item[0] == value), None)
            if model is None:
                self.error(request_id, -32602, f"Unknown model ID: {value}")
                return
            state["model"], state["level"] = value, model[3]
        elif option == "thought_level":
            model = next(item for item in MODELS if item[0] == state["model"])
            if value not in model[2]:
                self.error(
                    request_id,
                    -32602,
                    f'Thinking level "{value}" is not offered for model "{model[1]}"',
                )
                return
            state["level"] = value
        elif option == "mode":
            state["mode"] = value
        self.result(request_id, {"configOptions": self.options(sid)})
        self.update(
            sid, {"sessionUpdate": "config_option_update", "configOptions": self.options(sid)}
        )

    def on_session_set_mode(self, request_id: Any, params: dict[str, Any]) -> None:
        sid = str(params.get("sessionId"))
        self.sessions[sid]["mode"] = params.get("modeId")
        self.result(request_id, {})
        self.update(
            sid, {"sessionUpdate": "current_mode_update", "currentModeId": params.get("modeId")}
        )

    def on_session_close(self, request_id: Any, params: dict[str, Any]) -> None:
        self.sessions.pop(str(params.get("sessionId")), None)
        self.result(request_id, {})

    def on_session_cancel(self, request_id: Any, params: dict[str, Any]) -> None:
        self.cancel.set()

    def on_session_prompt(self, request_id: Any, params: dict[str, Any]) -> None:
        sid = str(params.get("sessionId"))
        if sid not in self.sessions:
            self.error(request_id, -32602, f"Session not found: {sid}")
            return
        if self.busy:
            if self.awaiting_answer:
                self.error(request_id, -32603, STEER_REFUSED.format(sid=sid))
                return
            self.prompt_waiters.put((request_id, params))
            return
        turns = self.config.get("turns", ["ok"])
        name = turns[self.turn_index] if self.turn_index < len(turns) else "ok"
        self.turn_index += 1
        self._count("turn", self.turn_index)
        self.cancel.clear()
        self.busy = True
        thread = threading.Thread(
            target=self.run, args=(name, request_id, sid, params), daemon=True
        )
        thread.start()

    # --------------------------------------------------------------- scenarios

    def run(self, name: str, request_id: Any, sid: str, params: dict[str, Any]) -> None:
        try:
            getattr(self, "turn_" + name.replace("-", "_"))(request_id, sid, params)
        finally:
            self.busy = False
            self.awaiting_answer = False

    def end(self, request_id: Any, reason: str = "end_turn", tokens: int = 100) -> None:
        self.result(
            request_id,
            {
                "stopReason": reason,
                "usage": {
                    "inputTokens": tokens,
                    "outputTokens": 2,
                    "cachedReadTokens": 0,
                    "cachedWriteTokens": 0,
                    "thoughtTokens": 0,
                    "totalTokens": tokens + 2,
                },
            },
        )

    def title(self, sid: str) -> None:
        if sid not in self.titled:
            self.titled.add(sid)
            self.update(
                sid, {"sessionUpdate": "session_info_update", "title": "Acknowledge Request"}
            )

    def chunks(self, sid: str, *texts: str) -> None:
        for text in texts:
            self.update(
                sid,
                {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": text}},
            )

    def used(self, sid: str, used: int) -> None:
        self.update(sid, {"sessionUpdate": "usage_update", "used": used, "size": WINDOW})

    def tool(self, sid: str, kind: str, status: str, call: dict[str, Any], **extra: Any) -> None:
        update = {"sessionUpdate": kind, "status": status, **call, **extra}
        self.update(sid, update)

    def turn_ok(self, request_id: Any, sid: str, params: dict[str, Any]) -> None:
        self.title(sid)
        self.chunks(sid, "O", "K")
        self.used(sid, 9523)
        self.end(request_id)

    def turn_compact(self, request_id: Any, sid: str, params: dict[str, Any]) -> None:
        self.used(sid, 17500)
        self.end(request_id, tokens=3000)

    def turn_fail(self, request_id: Any, sid: str, params: dict[str, Any]) -> None:
        self.error(request_id, -32603, "Model overloaded while reading /Users/someone/project/a.py")

    def turn_quota(self, request_id: Any, sid: str, params: dict[str, Any]) -> None:
        self.error(request_id, -32603, "You have exhausted your quota for this model")

    def turn_quota_text(self, request_id: Any, sid: str, params: dict[str, Any]) -> None:
        self.chunks(sid, "Usage Limit Reached for Gemini Models.")
        self.used(sid, 9600)
        self.end(request_id)

    def turn_exit(self, request_id: Any, sid: str, params: dict[str, Any]) -> None:
        self.chunks(sid, "Working")
        sys.stdout.flush()
        os._exit(1)

    def turn_hang(self, request_id: Any, sid: str, params: dict[str, Any]) -> None:
        """Runs until it is cancelled, as a long command does."""
        call = _command("call_9000", "sleep 300")
        self.tool(sid, "tool_call", "in_progress", call)
        while not self.cancel.wait(0.02):
            pass
        self.tool(sid, "tool_call_update", "failed", call, rawOutput={"output": ""})
        self.end(request_id, "cancelled")

    def turn_stuck(self, request_id: Any, sid: str, params: dict[str, Any]) -> None:
        """Never settles, cancelled or not."""
        time.sleep(600)

    def turn_tool(self, request_id: Any, sid: str, params: dict[str, Any]) -> None:
        call = _command("call_3127648591608400511", "ls -la")
        self.used(sid, 9659)
        self.tool(sid, "tool_call", "in_progress", call)
        self.tool(sid, "tool_call_update", "pending", call)
        # A test that needs the turn running before agy asks names a file agy waits for first.
        self.wait_for_file(self.config.get("ask_after"))
        self.awaiting_answer = True
        answer = self.ask(
            "session/request_permission",
            {
                "sessionId": sid,
                "toolCall": {"sessionUpdate": "tool_call", "status": "pending", **call},
                "options": [
                    {"optionId": "allow", "name": "Allow", "kind": "allow_once"},
                    {"optionId": "deny", "name": "Deny", "kind": "reject_once"},
                ],
            },
        )
        self.awaiting_answer = False
        if answer.get("cancelled") or self.cancel.is_set():
            self.tool(sid, "tool_call_update", "failed", call, rawOutput={"output": ""})
            self.end(request_id, "cancelled")
            return
        chosen = (answer.get("outcome") or {}).get("optionId")
        self.tool(sid, "tool_call_update", "in_progress", call)
        if chosen != "allow":
            self.tool(sid, "tool_call_update", "failed", call, rawOutput={"output": ""})
            self.end(request_id)
            return
        self.tool(
            sid,
            "tool_call_update",
            "completed",
            call,
            rawOutput={"output": "total 0\n.\n..\n", "exit_code": 0},
        )
        self.chunks(sid, "It printed", " 3 lines.")
        self.used(sid, 9811)
        self.end(request_id, tokens=19000)

    def turn_question(self, request_id: Any, sid: str, params: dict[str, Any]) -> None:
        call = {
            "toolCallId": "call_15383772450349901992",
            "name": "ask_question",
            "kind": "other",
            "title": "Run ask_question",
            "rawInput": {"questions": [{"question": "Which color do you prefer?"}]},
        }
        self.tool(sid, "tool_call", "in_progress", call)
        self.awaiting_answer = True
        answer = self.ask(
            "elicitation/create",
            {
                "sessionId": sid,
                "toolCallId": call["toolCallId"],
                "mode": "form",
                "message": "The agent has 2 questions.",
                "requestedSchema": {
                    "type": "object",
                    "properties": {
                        "q1": {
                            "type": "string",
                            "title": "Which color do you prefer?",
                            "oneOf": [
                                {"const": "1", "title": "(Recommended) red"},
                                {"const": "2", "title": "green"},
                            ],
                        },
                        "q1_other": {"type": "string", "title": "Other"},
                        "q2": {
                            "type": "array",
                            "title": "Which fruits do you like?",
                            "items": {
                                "anyOf": [
                                    {"const": "1", "title": "apple"},
                                    {"const": "2", "title": "banana"},
                                    {"const": "3", "title": "cherry"},
                                ]
                            },
                        },
                        "q2_other": {"type": "string", "title": "Other"},
                    },
                },
            },
        )
        self.awaiting_answer = False
        if answer.get("cancelled") or self.cancel.is_set():
            self.tool(sid, "tool_call_update", "failed", call)
            self.end(request_id, "cancelled")
            return
        self.tool(sid, "tool_call_update", "completed", call)
        self.chunks(sid, json.dumps(answer.get("content") or {}, sort_keys=True))
        self.used(sid, 10687)
        self.end(request_id)

    def turn_steer(self, request_id: Any, sid: str, params: dict[str, Any]) -> None:
        """A command runs; a second prompt takes the turn over and the first answers at once."""
        call = _command("call_13723896372879173286", "sleep 8")
        self.used(sid, 11550)
        self.tool(sid, "tool_call", "in_progress", call)
        try:
            second_id, _ = self.prompt_waiters.get(timeout=30)
        except queue.Empty:
            self.end(request_id)
            return
        self.end(request_id, tokens=11548)
        self.tool(
            sid,
            "tool_call_update",
            "completed",
            call,
            rawOutput={"output": "done", "exit_code": 0},
        )
        self.chunks(sid, "SLEPT", " STEERED")
        self.used(sid, 11722)
        self.end(second_id, tokens=11720)

    def turn_wake(self, request_id: Any, sid: str, params: dict[str, Any]) -> None:
        """A command goes on in the background; agy answers, then goes on by itself."""
        call = _command("call_412341763447906468", "sleep 12 && echo done", wait=500)
        self.used(sid, 12758)
        self.tool(sid, "tool_call", "in_progress", call)
        self.chunks(sid, "STARTED")
        self.used(sid, 12957)
        self.end(request_id)
        self.busy = False
        time.sleep(float(self.config.get("wake_delay", 0.3)))
        self.tool(
            sid,
            "tool_call_update",
            "completed",
            call,
            rawOutput={"output": "Command exited with code 0.\n\ndone\n", "exit_code": 0},
        )
        self.chunks(sid, "\n\nFINISHED")
        self.used(sid, 13213)

    def turn_schedule(self, request_id: Any, sid: str, params: dict[str, Any]) -> None:
        call = {
            "toolCallId": "call_14007289657061775369",
            "name": "schedule",
            "kind": "other",
            "title": "Run schedule",
            "rawInput": {"DurationSeconds": 60, "Prompt": "Reply with the single word PING"},
        }
        self.used(sid, 15209)
        self.tool(sid, "tool_call", "in_progress", call)
        self.chunks(sid, "I have scheduled a one-time timer.")
        self.used(sid, 15426)
        self.end(request_id)
        self.busy = False
        time.sleep(float(self.config.get("wake_delay", 0.3)))
        self.tool(sid, "tool_call_update", "completed", call)
        self.chunks(sid, "\n\nPING")
        self.used(sid, 15556)

    def turn_long(self, request_id: Any, sid: str, params: dict[str, Any]) -> None:
        """agy 1.5.15 and later: a background command keeps the prompt open until its result.

        As agy sends it: its first words, the command's result, then about 30 seconds later its
        reply to it, and only then the answer to the prompt, with every token of it.
        """
        call = _command("call_17734012458337888", "sleep 15 && echo slept")
        self.used(sid, 10740)
        self.tool(sid, "tool_call", "in_progress", call)
        self.tool(sid, "tool_call_update", "pending", call)
        if self.permission(sid, call) != "allow":
            self.tool(sid, "tool_call_update", "failed", call, rawOutput={"output": ""})
            self.end(request_id, "cancelled" if self.cancel.is_set() else "end_turn")
            return
        self.tool(sid, "tool_call_update", "in_progress", call)
        self.chunks(sid, "I", " have started running the command in the background.")
        self.used(sid, 10939)
        if self.cancel.wait(float(self.config.get("background_delay", 0.3))):
            self.tool(sid, "tool_call_update", "failed", call, rawOutput={"output": ""})
            self.end(request_id, "cancelled")
            return
        self.tool(
            sid,
            "tool_call_update",
            "completed",
            call,
            rawOutput={"output": "Command exited with code 0.\n\nslept\n", "exit_code": 0},
        )
        if self.cancel.wait(float(self.config.get("wake_delay", 0.3))):
            self.end(request_id, "cancelled")
            return
        self.chunks(sid, "\n\nThe command", " printed `slept`.")
        self.used(sid, 11175)
        self.end(request_id, tokens=15249)

    def turn_subagent(self, request_id: Any, sid: str, params: dict[str, Any]) -> None:
        """agy 1.5.16: a sub-agent's request reaches the client under the parent's session id.

        As agy sends it: the sub-agent starts at once, its command and its permission request
        come in the parent session, and the prompt answers after a silent tail.
        """
        spawn = {
            "toolCallId": "call_11825326914422831773",
            "name": "invoke_subagent",
            "kind": "other",
            "title": "Run invoke_subagent",
            "rawInput": {"Prompt": "Run exactly this shell command: echo sub-ok > sub.txt"},
        }
        self.used(sid, 11979)
        self.tool(sid, "tool_call", "in_progress", spawn)
        self.tool(sid, "tool_call_update", "completed", spawn)
        self.chunks(sid, "I have", " started a subagent to run the command.")
        child = _command("call_3209823881229263751", "echo sub-ok > sub.txt", wait=5000)
        self.tool(sid, "tool_call", "pending", child)
        chosen = self.permission(sid, child)
        if chosen == "allow":
            self.tool(sid, "tool_call_update", "in_progress", child)
            self.tool(
                sid,
                "tool_call_update",
                "completed",
                child,
                rawOutput={"output": "Command exited with code 0.\n", "exit_code": 0},
            )
        else:
            self.tool(sid, "tool_call_update", "failed", child, rawOutput={"output": ""})
        if self.cancel.is_set() or self.cancel.wait(float(self.config.get("wake_delay", 0.3))):
            self.end(request_id, "cancelled")
            return
        tasks = {
            "toolCallId": "call_9014473347655059",
            "name": "manage_task",
            "kind": "other",
            "title": "Run manage_task",
            "rawInput": {"Action": "list"},
        }
        self.used(sid, 12467)
        self.tool(sid, "tool_call", "in_progress", tasks)
        self.tool(sid, "tool_call_update", "completed", tasks)
        self.chunks(sid, "\n\nThe ", "`manage_task` tool lists 0 tasks.")
        self.used(sid, 12559)
        # Nothing at all, then the answer: the real one came a minute after the last words.
        if self.cancel.wait(float(self.config.get("tail_delay", 0.3))):
            self.end(request_id, "cancelled")
            return
        self.end(request_id, tokens=8970)

    def wait_for_file(self, name: str | None, timeout: float = 30.0) -> None:
        """Hold the turn until the test creates `name` in the fake's directory, or Stop comes."""
        if not name:
            return
        deadline = time.monotonic() + timeout
        while not (self.directory / name).exists() and time.monotonic() < deadline:
            if self.cancel.wait(0.02):
                return

    def permission(self, sid: str, call: dict[str, Any]) -> str | None:
        """Ask the client about `call` as agy does; the option it chose, or None."""
        self.awaiting_answer = True
        answer = self.ask(
            "session/request_permission",
            {
                "sessionId": sid,
                "toolCall": {"sessionUpdate": "tool_call", "status": "pending", **call},
                "options": [
                    {"optionId": "allow", "name": "Allow", "kind": "allow_once"},
                    {"optionId": "deny", "name": "Deny", "kind": "reject_once"},
                ],
            },
        )
        self.awaiting_answer = False
        if answer.get("cancelled") or self.cancel.is_set():
            return None
        return (answer.get("outcome") or {}).get("optionId")

    def turn_read(self, request_id: Any, sid: str, params: dict[str, Any]) -> None:
        """agy reads the file a prompt linked to, which the device allows itself."""
        link = next(
            (block for block in params.get("prompt", []) if block.get("type") == "resource_link"),
            None,
        )
        path = str(link["uri"])[len("file://") :] if link else "/nowhere"
        call = {
            "toolCallId": "call_10062955128929813487",
            "name": "read_file",
            "kind": "read",
            "title": "File ../attach/note.bin",
            "locations": [{"path": path}],
            "rawInput": {"Path": path},
        }
        self.tool(sid, "tool_call", "pending", call)
        answer = self.ask(
            "session/request_permission",
            {
                "sessionId": sid,
                "toolCall": {"sessionUpdate": "tool_call", "status": "pending", **call},
                "options": [
                    {"optionId": "allow", "name": "Allow", "kind": "allow_once"},
                    {"optionId": "deny", "name": "Deny", "kind": "reject_once"},
                ],
            },
        )
        chosen = (answer.get("outcome") or {}).get("optionId")
        self.tool(sid, "tool_call_update", "completed" if chosen == "allow" else "failed", call)
        self.chunks(sid, f"READ {chosen}")
        self.used(sid, 12702)
        self.end(request_id)

    # ---------------------------------------------------------------------- run

    def serve(self) -> None:
        for line in sys.stdin:
            if not line.strip():
                continue
            message = json.loads(line)
            self.log(message)
            self.handle(message)


def _command(call_id: str, command: str, wait: int = 10000) -> dict[str, Any]:
    raw = {"CommandLine": command, "IsDaemon": False, "WaitMsBeforeAsync": wait}
    return {
        "toolCallId": call_id,
        "name": "run_command",
        "kind": "execute",
        "title": command,
        "args": raw,
        "rawInput": raw,
    }


def main() -> None:
    directory = Path(os.environ["RC_FAKE_AGY_DIR"])
    config = json.loads((directory / "config.json").read_text())
    argv = sys.argv[1:]
    with (directory / "starts.jsonl").open("a") as handle:
        handle.write(json.dumps({"argv": argv, "cwd": os.getcwd()}) + "\n")
    if argv == ["--version"]:
        print(f"agy {config.get('version', '1.5.14')}")
        return
    if argv[:2] == ["-p", "/usage"]:
        if config.get("usage_fails"):
            sys.exit(1)
        print(json.dumps(config.get("usage", USAGE)))
        return
    if argv == ["acp"]:
        Fake(directory).serve()
        return
    sys.exit(2)


if __name__ == "__main__":
    main()
