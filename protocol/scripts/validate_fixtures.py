#!/usr/bin/env python3
"""Validate every remote-control protocol fixture against the JSON Schemas.

Run from the protocol directory:

    uv run --with jsonschema python scripts/validate_fixtures.py

Three things are checked:

1. Schema conformance - every file under fixtures/ validates against the schema
   selected by its directory and name.
2. Semantics that JSON Schema cannot express - strictly increasing seq inside a
   timeline, the session.history rules of PROTOCOL.md section 8, event and
   tool-payload byte bounds, and UUID v4 identifiers.
3. Coverage and self-test - every frame type, event kind, tool kind and HTTP body
   named in the protocol has both a schema entry and a fixture, and every file
   under fixtures_invalid/ is rejected.

Exit status is 0 only when all three pass.
"""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

ROOT = Path(__file__).resolve().parent.parent
SCHEMA_DIR = ROOT / "schema"
FIXTURE_DIR = ROOT / "fixtures"
INVALID_DIR = ROOT / "fixtures_invalid"
SCHEMA_BASE = "https://remote-control.dev/schema/v1/"

MAX_EVENT_BYTES = 64 * 1024
MAX_TOOL_INPUT_BYTES = 8 * 1024
MAX_TOOL_OUTPUT_BYTES = 16 * 1024
MAX_PATCH_BYTES = 32 * 1024

UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
UUID_FIELDS = frozenset(
    {"device_id", "session_id", "turn_id", "request_id", "block_id", "parent_block_id", "queued_id"}
)

# --- protocol inventory (PROTOCOL.md; must stay in step with the frozen contract) ---

APP_FRAME_TYPES = (
    "hello",
    "device.updated",
    "device.removed",
    "session.updated",
    "session.removed",
    "session.event",
    "pairing.progress",
    "preferences.updated",
    "terminal.output",
    "terminal.exited",
    "ping",
    "pong",
    "reply",
    "session.subscribe",
    "session.unsubscribe",
    "session.seen",
    "session.create",
    "session.send",
    "session.stop",
    "session.approve",
    "session.answer",
    "session.set",
    "session.history",
    "session.block",
    "session.commands",
    "session.command",
    "session.queue_remove",
    "session.resume_set",
    "session.resume_cancel",
    "session.takeover",
    "session.archive",
    "session.delete",
    "device.dirs",
    "device.git",
    "device.mkdir",
    "terminal.open",
    "terminal.input",
    "terminal.resize",
    "terminal.attach",
    "terminal.close",
    "device.agents",
    "device.update",
)
DEVICE_FRAME_TYPES = (
    "hello",
    "hello_ack",
    "preferences",
    "session.updated",
    "session.removed",
    "session.event",
    "agents.updated",
    "update.failed",
    "terminal.output",
    "terminal.exited",
    "ping",
    "pong",
    "reply",
)
FORWARDED_TYPES = (
    "session.create",
    "session.send",
    "session.stop",
    "session.approve",
    "session.answer",
    "session.set",
    "session.history",
    "session.block",
    "session.commands",
    "session.command",
    "session.queue_remove",
    "session.resume_set",
    "session.resume_cancel",
    "session.takeover",
    "session.archive",
    "session.delete",
    "device.dirs",
    "device.git",
    "device.mkdir",
    "terminal.open",
    "terminal.input",
    "terminal.resize",
    "terminal.attach",
    "terminal.close",
    "terminal.detach",
    "device.agents",
    "device.update",
)
STT_FRAME_TYPES = ("stt.stop", "stt.cancel", "stt.partial", "stt.final", "stt.error")
EVENT_KINDS = (
    "user_message",
    "assistant_text",
    "thinking",
    "tool_call",
    "todos",
    "approval",
    "question",
    "turn_started",
    "turn_completed",
    "status",
    "meta",
    "queue",
    "notice",
    "error",
    "resume",
)
TOOL_KINDS = (
    "shell",
    "read",
    "edit",
    "write",
    "search",
    "web",
    "mcp",
    "subagent",
    "todo",
    "other",
)

# fixtures/http/<stem>.json -> http.json definition
HTTP_DEFS = {
    "health.response": "HealthResponse",
    "login.request": "LoginRequest",
    "login.response": "LoginResponse",
    "login.error": "ErrorResponse",
    "logout.response": "OkResponse",
    "auth.session.response": "AuthSessionResponse",
    "register.request": "RegisterRequest",
    "password.request": "PasswordChangeRequest",
    "users.list.response": "UserListResponse",
    "users.create.request": "UserCreateRequest",
    "users.patch.request": "UserPatchRequest",
    "users.response": "UserResponse",
    "registration.patch.request": "RegistrationPatchRequest",
    "registration.response": "RegistrationResponse",
    "config.response": "ConfigResponse",
    "preferences.response": "PreferencesResponse",
    "preferences.patch.request": "PreferencesPatchRequest",
    "devices.enroll.request": "EnrollRequest",
    "devices.enroll.response": "EnrollResponse",
    "devices.list.response": "DeviceListResponse",
    "devices.patch.request": "DevicePatchRequest",
    "devices.patch.response": "DeviceResponse",
    "devices.delete.response": "OkResponse",
    "devices.pairing.response": "PairingResponse",
    "devices.pairing.delete.response": "OkResponse",
    "devices.pairing.request.response": "PairingRequestResponse",
    "devices.pairing.request.status.response": "PairingRequestStatusResponse",
    "devices.pairing.claim.response": "PairingClaimResponse",
    "sessions.list.response": "SessionListResponse",
    "stt.transcribe.response": "SttTranscribeResponse",
    "stt.transcribe.error": "ErrorResponse",
    "polish.models.response": "PolishModelsResponse",
    "polish.request": "PolishRequest",
    "polish.response": "PolishResponse",
    "push.web.vapid.response": "VapidResponse",
    "push.web.subscribe.request": "WebPushSubscribeRequest",
    "push.web.unsubscribe.request": "WebPushUnsubscribeRequest",
    "push.apns.register.request": "ApnsRegisterRequest",
    "push.apns.unregister.request": "ApnsUnregisterRequest",
    "push.payload": "PushPayload",
    "push.payload.limit": "PushPayload",
    "push.payload.badge": "PushPayload",
}

# fixture directory -> (schema file, definition or None for the file's root)
DIR_SCHEMAS = {
    "app": ("app_frames.json", None),
    "device": ("device_frames.json", None),
    "device/forwarded": ("device_frames.json", "ForwardedRequest"),
    "events": ("events.json", None),
    "stt": ("stt_frames.json", None),
    "timelines": ("timeline.json", None),
    "replay": ("app_frames.json", "ReplySessionSubscribe"),
}
PATH_SCHEMAS = {
    "history/page.json": ("app_frames.json", "ReplySessionHistory"),
    "objects/session.shared-idle.json": ("objects.json", "Session"),
    "objects/session.shared-running.json": ("objects.json", "Session"),
    "objects/agent.claude-attach.json": ("objects.json", "AgentInfo"),
    "objects/agent.codex-daemon.json": ("objects.json", "AgentInfo"),
    "objects/session.codex-shared-running.json": ("objects.json", "Session"),
    "objects/agent.grok.json": ("objects.json", "AgentInfo"),
    "objects/agent.pi.json": ("objects.json", "AgentInfo"),
    "objects/session.resume-pending.json": ("objects.json", "Session"),
    "app/reply.session.create.json": ("app_frames.json", "ReplySessionCreate"),
    "app/reply.session.send.json": ("app_frames.json", "ReplySessionSend"),
    "app/reply.session.set.json": ("app_frames.json", "ReplySessionSet"),
    "app/reply.session.block.json": ("app_frames.json", "ReplySessionBlock"),
    "app/reply.session.commands.json": ("app_frames.json", "ReplySessionCommands"),
    "app/reply.session.takeover.json": ("app_frames.json", "ReplySessionTakeover"),
    "app/reply.session.archive.json": ("app_frames.json", "ReplySessionArchive"),
    "app/reply.session.resume_set.json": ("app_frames.json", "ReplySessionResumeSet"),
    "app/reply.device.dirs.json": ("app_frames.json", "ReplyDeviceDirs"),
    "app/reply.device.git.json": ("app_frames.json", "ReplyDeviceGit"),
    "app/reply.device.mkdir.json": ("app_frames.json", "ReplyDeviceMkdir"),
    "app/reply.terminal.open.json": ("app_frames.json", "ReplyTerminalOpen"),
    "app/reply.terminal.attach.json": ("app_frames.json", "ReplyTerminalAttach"),
    "app/reply.device.agents.json": ("app_frames.json", "ReplyDeviceAgents"),
    "app/reply.device.update.json": ("app_frames.json", "ReplyDeviceUpdate"),
}
# fixtures_invalid/<prefix>__<reason>.json -> (schema file, definition or None)
INVALID_SCHEMAS = {
    "app": ("app_frames.json", None),
    "device": ("device_frames.json", None),
    "events": ("events.json", None),
    "stt": ("stt_frames.json", None),
    "timeline": ("timeline.json", None),
    "history": ("app_frames.json", "ReplySessionHistory"),
    "http.enroll": ("http.json", "EnrollRequest"),
    "http.login": ("http.json", "LoginRequest"),
    "http.preferences": ("http.json", "PreferencesPatchRequest"),
    "objects.agent": ("objects.json", "AgentInfo"),
    "objects.user": ("objects.json", "User"),
}


@dataclass
class Result:
    path: Path
    schema: str
    problems: list[str] = field(default_factory=list)
    note: str = ""

    @property
    def ok(self) -> bool:
        return not self.problems


def load_registry() -> Registry:
    resources = []
    for path in sorted(SCHEMA_DIR.glob("*.json")):
        contents = json.loads(path.read_text())
        resource = Resource.from_contents(contents, default_specification=DRAFT202012)
        resources.append((contents["$id"], resource))
    return Registry().with_resources(resources)


def validator_for(
    registry: Registry, schema_file: str, definition: str | None
) -> Draft202012Validator:
    uri = SCHEMA_BASE + schema_file
    if definition:
        uri = f"{uri}#/$defs/{definition}"
    return Draft202012Validator({"$ref": uri}, registry=registry)


def schema_label(schema_file: str, definition: str | None) -> str:
    return f"{schema_file}#/$defs/{definition}" if definition else schema_file


def select_schema(relative: Path) -> tuple[str, str | None]:
    key = relative.as_posix()
    if key in PATH_SCHEMAS:
        return PATH_SCHEMAS[key]
    parent = relative.parent.as_posix()
    if parent == "http":
        stem = relative.name[: -len(".json")]
        definition = HTTP_DEFS.get(stem)
        if definition is None:
            raise KeyError(f"no http.json definition mapped for fixtures/http/{relative.name}")
        return "http.json", definition
    if parent in DIR_SCHEMAS:
        return DIR_SCHEMAS[parent]
    raise KeyError(f"no schema mapped for fixtures/{key}")


def schema_problems(validator: Draft202012Validator, document: Any) -> list[str]:
    problems = []
    for error in sorted(validator.iter_errors(document), key=lambda e: list(e.absolute_path)):
        where = "/".join(str(p) for p in error.absolute_path) or "<root>"
        problems.append(f"schema: {where}: {error.message.splitlines()[0][:160]}")
    return problems


def walk(node: Any, path: str = "") -> Iterator[tuple[str, str, Any]]:
    """Yield (json pointer, key, value) for every mapping entry in a document."""
    if isinstance(node, dict):
        for key, value in node.items():
            here = f"{path}/{key}"
            yield here, key, value
            yield from walk(value, here)
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from walk(value, f"{path}/{index}")


def check_uuids(document: Any) -> list[str]:
    problems = []
    for pointer, key, value in walk(document):
        if key in UUID_FIELDS and isinstance(value, str) and not UUID_RE.match(value):
            problems.append(f"ids: {pointer} is not a UUID v4: {value!r}")
    return problems


def is_event(node: Any) -> bool:
    return isinstance(node, dict) and "seq" in node and "kind" in node


def event_nodes(document: Any) -> list[tuple[str, dict[str, Any]]]:
    """Every event in the document, as (json pointer, event)."""
    found: list[tuple[str, dict[str, Any]]] = []
    if is_event(document):
        found.append(("<root>", document))
    found += [(pointer, value) for pointer, _, value in walk(document) if is_event(value)]
    return found


def check_event_bounds(document: Any) -> list[str]:
    """Enforce the byte bounds of PROTOCOL.md section 2.6 on every event in the document."""
    problems: list[str] = []
    for pointer, value in event_nodes(document):
        size = len(json.dumps(value, ensure_ascii=False).encode())
        if size > MAX_EVENT_BYTES:
            problems.append(f"bounds: {pointer} event is {size} B, over the 64 KiB frame limit")
        if value.get("kind") != "tool_call":
            continue
        if "input" in value:
            size = len(json.dumps(value["input"], ensure_ascii=False).encode())
            if size > MAX_TOOL_INPUT_BYTES and not value.get("input_truncated"):
                problems.append(f"bounds: {pointer}/input is {size} B without input_truncated")
        if isinstance(value.get("output"), str):
            size = len(value["output"].encode())
            if size > MAX_TOOL_OUTPUT_BYTES and not value.get("output_truncated"):
                problems.append(f"bounds: {pointer}/output is {size} B without output_truncated")
        diff = value.get("diff")
        if isinstance(diff, dict) and isinstance(diff.get("patch"), str):
            size = len(diff["patch"].encode())
            if size > MAX_PATCH_BYTES and not diff.get("patch_truncated"):
                problems.append(f"bounds: {pointer}/diff/patch is {size} B without patch_truncated")
    return problems


def check_first_seq(document: Any) -> list[str]:
    """Amendment A8: first_seq is where the block started, so it never follows seq."""
    problems: list[str] = []
    for pointer, value in event_nodes(document):
        first = value.get("first_seq")
        seq = value.get("seq")
        if first is None:
            continue
        if not isinstance(first, int) or isinstance(first, bool):
            problems.append(f"first_seq: {pointer} first_seq is not an integer")
        elif isinstance(seq, int) and first > seq:
            problems.append(f"first_seq: {pointer} first_seq {first} is after seq {seq}")
    return problems


def check_block_start(
    event: dict[str, Any], index: int, first_seen: dict[str, int], seq: int
) -> list[str]:
    """Amendment A8: a block's first event may only claim itself; later events must agree."""
    block_id = event.get("block_id")
    if not isinstance(block_id, str):
        return []
    first = event.get("first_seq")
    if block_id not in first_seen:
        first_seen[block_id] = seq
        if first is not None and first != seq:
            return [
                f"timeline: frames/{index} opens a block at seq {seq} but claims first_seq {first}"
            ]
        return []
    if first is not None and first != first_seen[block_id]:
        opened = first_seen[block_id]
        return [f"timeline: frames/{index} claims first_seq {first} for a block opened at {opened}"]
    return []


def check_timeline(document: Any) -> list[str]:
    problems: list[str] = []
    frames = document.get("frames") if isinstance(document, dict) else None
    if not isinstance(frames, list):
        return problems
    session_id = document.get("session", {}).get("session_id")
    previous = -1
    first_seen: dict[str, int] = {}
    for index, frame in enumerate(frames):
        event = frame.get("event", {}) if isinstance(frame, dict) else {}
        seq = event.get("seq")
        if not isinstance(seq, int):
            problems.append(f"timeline: frames/{index} has no integer seq")
            continue
        if seq <= previous:
            problems.append(f"timeline: frames/{index} seq {seq} does not increase past {previous}")
        previous = seq
        if frame.get("session_id") != session_id:
            problems.append(
                f"timeline: frames/{index} session_id does not match the session summary"
            )
        problems += check_block_start(event, index, first_seen, seq)
    last_seq = document.get("session_final", {}).get("last_seq")
    if isinstance(last_seq, int) and previous >= 0 and last_seq != previous:
        problems.append(
            f"timeline: session_final.last_seq is {last_seq}, last event seq is {previous}"
        )
    return problems


HISTORY_EXCLUDED_KINDS = frozenset({"status", "meta", "queue"})


def check_history_page(document: Any) -> list[str]:
    """PROTOCOL.md section 8: latest event per block, ascending seq, no deltas or state kinds."""
    problems = []
    events = document.get("result", {}).get("events", []) if isinstance(document, dict) else []
    previous = -1
    seen_blocks: dict[str, int] = {}
    for index, event in enumerate(events):
        kind = event.get("kind")
        seq = event.get("seq")
        if isinstance(seq, int):
            if seq <= previous:
                problems.append(
                    f"history: events/{index} seq {seq} does not increase past {previous}"
                )
            previous = seq
        if kind in HISTORY_EXCLUDED_KINDS:
            problems.append(f"history: events/{index} kind {kind!r} must not appear in history")
        if "delta" in event:
            problems.append(f"history: events/{index} carries a streaming delta")
        if kind in {"assistant_text", "thinking"} and event.get("done") is not True:
            problems.append(f"history: events/{index} kind {kind!r} is not a final event")
        block_id = event.get("block_id")
        if isinstance(block_id, str):
            if block_id in seen_blocks:
                problems.append(
                    f"history: events/{index} repeats block {block_id} first seen at events/{seen_blocks[block_id]}"
                )
            seen_blocks[block_id] = index
    return problems


def check_document(
    path: Path, registry: Registry, schema_file: str, definition: str | None
) -> Result:
    result = Result(path=path, schema=schema_label(schema_file, definition))
    try:
        document = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        result.problems.append(f"json: {exc}")
        return result
    result.problems += schema_problems(validator_for(registry, schema_file, definition), document)
    result.problems += check_uuids(document)
    result.problems += check_event_bounds(document)
    result.problems += check_first_seq(document)
    if schema_file == "timeline.json":
        result.problems += check_timeline(document)
    if definition == "ReplySessionHistory":
        result.problems += check_history_page(document)
    return result


def consts_in(schema: Any) -> set[Any]:
    """Every literal a schema pins, from `const` and from `enum` members."""
    found: set[Any] = set()
    if isinstance(schema, dict):
        if "const" in schema:
            found.add(schema["const"])
        if isinstance(schema.get("enum"), list):
            found.update(schema["enum"])
        for value in schema.values():
            found |= consts_in(value)
    elif isinstance(schema, list):
        for value in schema:
            found |= consts_in(value)
    return found


def collect_values(documents: list[Any], key: str) -> set[Any]:
    found: set[Any] = set()
    for document in documents:
        for _, name, value in walk(document):
            if name == key and isinstance(value, str):
                found.add(value)
        if isinstance(document, dict) and isinstance(document.get(key), str):
            found.add(document[key])
    return found


def check_coverage(documents_by_dir: dict[str, list[Any]]) -> list[str]:
    problems = []
    schemas = {p.name: json.loads(p.read_text()) for p in SCHEMA_DIR.glob("*.json")}

    inventories = (
        ("app frame", APP_FRAME_TYPES, "app_frames.json", ["app"], "type"),
        ("device frame", DEVICE_FRAME_TYPES, "device_frames.json", ["device"], "type"),
        ("forwarded frame", FORWARDED_TYPES, "app_frames.json", ["device/forwarded"], "type"),
        ("stt frame", STT_FRAME_TYPES, "stt_frames.json", ["stt"], "type"),
        (
            "event kind",
            EVENT_KINDS,
            "events.json",
            ["events", "timelines", "history", "replay"],
            "kind",
        ),
        ("tool kind", TOOL_KINDS, "events.json", ["events", "timelines"], "tool_kind"),
    )
    for label, inventory, schema_name, dirs, key in inventories:
        declared = consts_in(schemas[schema_name])
        documents = [d for directory in dirs for d in documents_by_dir.get(directory, [])]
        used = collect_values(documents, key)
        for name in inventory:
            if name not in declared:
                problems.append(f"coverage: {label} {name!r} has no const in {schema_name}")
            if name not in used:
                problems.append(f"coverage: {label} {name!r} has no fixture in {'/'.join(dirs)}")

    http_fixtures = {p.name[: -len(".json")] for p in (FIXTURE_DIR / "http").glob("*.json")}
    http_defs = set(schemas["http.json"]["$defs"])
    for stem, definition in HTTP_DEFS.items():
        if definition not in http_defs:
            problems.append(f"coverage: http body {definition!r} is not defined in http.json")
        if stem not in http_fixtures:
            problems.append(f"coverage: http body {definition!r} has no fixtures/http/{stem}.json")
    for definition in sorted(http_defs):
        if definition not in set(HTTP_DEFS.values()):
            problems.append(f"coverage: http.json defines {definition!r} but no fixture uses it")
    return problems


def print_table(results: list[Result], title: str) -> None:
    if not results:
        return
    print(f"\n{title}")
    width = max(len(str(r.path.relative_to(ROOT))) for r in results)
    for result in sorted(results, key=lambda r: str(r.path)):
        status = "PASS" if result.ok else "FAIL"
        print(f"  {status}  {result.path.relative_to(ROOT)!s:<{width}}  {result.schema}")
        if result.note:
            print(f"          rejected: {result.note}")
        for problem in result.problems:
            print(f"          - {problem}")


def validate_fixtures(registry: Registry) -> tuple[list[Result], dict[str, list[Any]]]:
    results: list[Result] = []
    documents_by_dir: dict[str, list[Any]] = {}
    for path in sorted(FIXTURE_DIR.rglob("*.json")):
        relative = path.relative_to(FIXTURE_DIR)
        schema_file, definition = select_schema(relative)
        results.append(check_document(path, registry, schema_file, definition))
        try:
            documents_by_dir.setdefault(relative.parent.as_posix(), []).append(
                json.loads(path.read_text())
            )
        except json.JSONDecodeError:
            pass
    return results, documents_by_dir


def self_test(registry: Registry) -> list[Result]:
    """Every fixture under fixtures_invalid/ must be rejected by the same pipeline."""
    negatives: list[Result] = []
    for path in sorted(INVALID_DIR.glob("*.json")):
        prefix = path.name.split("__", 1)[0]
        if prefix not in INVALID_SCHEMAS:
            negatives.append(Result(path, "?", [f"self-test: unknown schema prefix {prefix!r}"]))
            continue
        schema_file, definition = INVALID_SCHEMAS[prefix]
        outcome = check_document(path, registry, schema_file, definition)
        if outcome.problems:
            negatives.append(Result(path, outcome.schema, []))
            negatives[-1].note = outcome.problems[0]
        else:
            negatives.append(
                Result(path, outcome.schema, ["self-test: this invalid fixture was accepted"])
            )
    return negatives


def main() -> int:
    registry = load_registry()
    results, documents_by_dir = validate_fixtures(registry)
    print_table(results, f"fixtures ({len(results)} files)")

    negatives = self_test(registry)
    print_table(negatives, f"fixtures_invalid, must be rejected ({len(negatives)} files)")

    coverage = check_coverage(documents_by_dir)
    print("\ncoverage")
    if coverage:
        for problem in coverage:
            print(f"  FAIL  {problem}")
    else:
        print(
            f"  PASS  {len(APP_FRAME_TYPES)} app frames, {len(DEVICE_FRAME_TYPES)} device frames, "
            f"{len(FORWARDED_TYPES)} forwarded, {len(STT_FRAME_TYPES)} stt frames, "
            f"{len(EVENT_KINDS)} event kinds, {len(TOOL_KINDS)} tool kinds, "
            f"{len(set(HTTP_DEFS.values()))} http bodies"
        )

    failures = [r for r in results + negatives if not r.ok]
    print(
        f"\n{len(results)} fixtures, {len(negatives)} negative cases, "
        f"{len(failures) + len(coverage)} problems"
    )
    return 1 if failures or coverage else 0


if __name__ == "__main__":
    sys.exit(main())
