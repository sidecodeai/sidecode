"""A fake `pi --mode rpc` peer: real JSONL framing, documented replies.

The runner spawns this through a shim exactly as it spawns pi, so the
subprocess, the framing and the reader task are all exercised. What it answers
comes from the files beside it, written from pi's shipped `docs/rpc.md`.

`RC_FAKE_PI_DIR` names a scratch directory: `mode` picks the scenario and every
command the peer receives is appended to `commands.jsonl`.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
# Where the turn's events stop when a scenario has to leave one running: after
# the first tool call has started and before anything settles.
STALL_AT = "tool_execution_start"


def _rows(name: str) -> list[dict[str, Any]]:
    text = (HERE / name).read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def _object(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((HERE / name).read_text(encoding="utf-8"))
    return data


class FakePi:
    def __init__(self) -> None:
        self.directory = Path(os.environ["RC_FAKE_PI_DIR"])
        self.mode = (self.directory / "mode").read_text(encoding="utf-8").strip()
        self.log = (self.directory / "commands.jsonl").open("a", encoding="utf-8")
        self.session_id = _argument("--session-id")
        self.steering: list[str] = []

    # ------------------------------------------------------------------ wire

    def send(self, payload: dict[str, Any]) -> None:
        sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
        sys.stdout.flush()

    def respond(self, message: dict[str, Any], data: dict[str, Any] | None = None) -> None:
        payload: dict[str, Any] = {
            "id": message.get("id"),
            "type": "response",
            "command": message.get("type"),
            "success": True,
        }
        if data is not None:
            payload["data"] = data
        self.send(payload)

    def refuse(self, message: dict[str, Any], error: str) -> None:
        self.send(
            {
                "id": message.get("id"),
                "type": "response",
                "command": message.get("type"),
                "success": False,
                "error": error,
            }
        )

    def record(self, message: dict[str, Any]) -> None:
        self.log.write(json.dumps(message, ensure_ascii=False) + "\n")
        self.log.flush()

    # ------------------------------------------------------------- scenarios

    def run(self) -> None:
        for line in sys.stdin:
            if not line.strip():
                continue
            message = json.loads(line)
            self.record(message)
            self.dispatch(message)

    def dispatch(self, message: dict[str, Any]) -> None:
        command = str(message.get("type") or "")
        if command == "get_state":
            self.respond(message, self.state())
        elif command == "get_session_stats":
            self.respond(message, self.stats())
        elif command == "prompt":
            self.prompt(message)
        elif command == "clear_queue":
            cleared = {"steering": list(self.steering), "followUp": []}
            self.steering.clear()
            self.respond(message, cleared)
            self.send({"type": "queue_update", "steering": [], "followUp": []})
        elif command == "abort":
            self.respond(message)
            self.send({"type": "agent_settled"})
        elif command == "set_model":
            self.set_model(message)
        elif command == "set_thinking_level":
            self.respond(message)
        elif command == "get_commands":
            self.respond(message, self.available())
        elif command == "compact":
            self.compact(message)
        else:
            self.refuse(message, f"Unknown command: {command}")

    def available(self) -> dict[str, Any]:
        """`get_commands`, with the fixture's paths pointing into the scratch."""
        text = (HERE / "commands.json").read_text(encoding="utf-8")
        data: dict[str, Any] = json.loads(text.replace("{dir}", str(self.directory)))
        return data

    def compact(self, message: dict[str, Any]) -> None:
        """pi emits the compaction's own events before it answers the command."""
        self.send({"type": "compaction_start", "reason": "manual"})
        if self.mode == "refuse":
            failure = "Nothing to compact (session too small)"
            self.send(
                {
                    "type": "compaction_end",
                    "reason": "manual",
                    "aborted": False,
                    "willRetry": False,
                    "errorMessage": f"Compaction failed: {failure}",
                }
            )
            self.refuse(message, failure)
            return
        self.send(
            {
                "type": "compaction_end",
                "reason": "manual",
                "result": {"summary": "Earlier turns, summarised."},
                "aborted": False,
                "willRetry": False,
            }
        )
        self.respond(message, {"summary": "Earlier turns, summarised."})

    def state(self) -> dict[str, Any]:
        state = _object("state.json")
        state["sessionId"] = "reassigned-by-pi" if self.mode == "rename" else self.session_id
        return state

    def stats(self) -> dict[str, Any]:
        stats = _object("stats.json")
        stats["sessionId"] = self.session_id
        return stats

    def set_model(self, message: dict[str, Any]) -> None:
        model = f"{message.get('provider')}/{message.get('modelId')}"
        if message.get("modelId") == "nope":
            self.refuse(message, f"Model not found: {model}")
            return
        self.respond(message, {"id": str(message.get("modelId")), "provider": "anthropic"})

    def prompt(self, message: dict[str, Any]) -> None:
        if self.mode == "refuse":
            self.refuse(message, "No API key found for the selected model.")
            return
        behaviour = str(message.get("streamingBehavior") or "")
        if behaviour == "steer":
            self.steer(message)
            return
        self.respond(message)
        self.emit_turn(stall=self.mode in {"stall", "steer", "crash"})
        if self.mode == "crash":
            # pi killed, or gone on a fatal error, with the turn still running.
            raise SystemExit(0)

    def steer(self, message: dict[str, Any]) -> None:
        text = str(message.get("message") or "")
        self.steering.append(text)
        self.respond(message)
        self.send({"type": "queue_update", "steering": list(self.steering), "followUp": []})
        if self.mode == "stall":
            return
        # The agent reads the message at its next step, which empties the queue
        # and lets the rest of the turn run.
        self.steering.clear()
        self.send({"type": "queue_update", "steering": [], "followUp": []})
        self.emit_turn(stall=False, skip_to=STALL_AT)

    def emit_turn(self, *, stall: bool, skip_to: str | None = None) -> None:
        rows = _rows("turn.jsonl")
        reached = skip_to is None
        for row in rows:
            kind = str(row.get("type") or "")
            if not reached:
                reached = kind == skip_to
                if not reached:
                    continue
            if stall and kind == STALL_AT:
                return
            self.send(row)


def _argument(name: str) -> str:
    argv = sys.argv[1:]
    for index, value in enumerate(argv):
        if value == name and index + 1 < len(argv):
            return argv[index + 1]
    return ""


def main() -> int:
    peer = FakePi()
    try:
        peer.run()
    except (BrokenPipeError, KeyboardInterrupt):
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
