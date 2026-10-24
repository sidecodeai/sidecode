"""Reading the daemon's thread index, and the A11 section 4.4 origin/control table."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ....models import MAX_TITLE, now_ms
from ..provenance import owned_here

# Every real turn spawns a short-lived thread that only generates a title; those
# would otherwise flicker into the session list on every message.
EPHEMERAL = "ephemeral"


def is_ephemeral(thread: dict[str, Any]) -> bool:
    return bool(thread.get(EPHEMERAL))


def is_ours(thread: dict[str, Any]) -> bool:
    """Whether an index entry describes a thread this device may publish (A18)."""
    return owned_here(thread.get("originator"), thread.get("source"))


def is_active(status: Any) -> bool:
    """Whether a `ThreadStatus` says a turn is in progress."""
    return isinstance(status, dict) and status.get("type") == "active"


def parent_of(thread: dict[str, Any]) -> str | None:
    """The thread that spawned this one, when a subagent runs in it.

    Codex names the parent on the thread itself (`parentThreadId`, beside
    `forkedFromId`) and again inside the `source` object it gives a subagent,
    whose variants spell it `parent_thread_id`. Either one is the parent;
    everything else is a thread somebody opened for themselves.
    """
    direct = _text(thread.get("parentThreadId")) or _text(thread.get("parent_thread_id"))
    return direct or _nested_parent(thread.get("source"), 3)


def _nested_parent(value: Any, depth: int) -> str | None:
    """Find the parent thread id in the variant object a `source` may be."""
    if depth <= 0 or not isinstance(value, dict):
        return None
    named = _text(value.get("parentThreadId")) or _text(value.get("parent_thread_id"))
    if named:
        return named
    for nested in value.values():
        found = _nested_parent(nested, depth - 1)
        if found:
            return found
    return None


def resolve(
    created_here: bool,
    loaded: bool,
    terminal_holds: bool,
    local_turn: bool = False,
) -> tuple[str, str]:
    """`(origin, control)` for one Codex thread on the daemon.

    An unloaded thread is `none` and the next `session.send` resumes it. A
    loaded thread is `shared` only while a terminal is known to be in it, which
    the daemon never reports: `terminal_holds` is what the service has pieced
    together from the thread's own evidence and the process scan. "Loaded" on
    its own says nothing, because the daemon never unloads a thread — a
    directory holds every thread ever opened in it — so taking it for a
    terminal would put a row in the app for every `codex` the user has ever
    run there. Once the terminal is gone the thread stays ours to drive —
    `remote` while a turn this device started is running, `none` when it is
    idle — and `origin` never changes.
    """
    origin = "remote" if created_here else "terminal"
    if not loaded:
        return origin, "none"
    if terminal_holds:
        return origin, "shared"
    if created_here or local_turn:
        return origin, "remote"
    return origin, "none"


@dataclass(slots=True)
class ThreadSummary:
    """The fields of a daemon thread a session record is built from."""

    thread_id: str
    cwd: str
    title: str
    # The thread's own name, which Codex generates from the conversation. Empty
    # when it has none yet, and then `title` falls back to the first prompt.
    name: str
    model: str | None
    effort: str | None
    # Unix milliseconds, as everywhere in the protocol; Codex itself counts seconds.
    created_at: int
    updated_at: int
    active: bool | None
    # The client that opened the thread and where it sits, exactly as the index
    # reports them. A `source` the index gives as an object — a subagent — is
    # kept as `None`, which reads as foreign like every other unknown shape.
    originator: str | None
    source: str | None

    @property
    def ours(self) -> bool:
        """Whether this thread is the device's to publish (A18)."""
        return owned_here(self.originator, self.source)

    @classmethod
    def parse(cls, thread: dict[str, Any]) -> ThreadSummary | None:
        thread_id = str(thread.get("id") or thread.get("sessionId") or "")
        if not thread_id:
            return None
        name = str(thread.get("name") or "").strip()
        preview = str(thread.get("preview") or "").strip()
        title = (name or preview.splitlines()[0] if preview or name else "")[:MAX_TITLE]
        created = _millis(thread.get("createdAt")) or now_ms()
        updated = _millis(thread.get("updatedAt")) or created
        model = thread.get("model")
        effort = thread.get("reasoningEffort")
        return cls(
            thread_id=thread_id,
            cwd=str(thread.get("cwd") or ""),
            title=title,
            name=name[:MAX_TITLE],
            model=model if isinstance(model, str) and model else None,
            effort=effort if isinstance(effort, str) and effort else None,
            created_at=created,
            updated_at=updated,
            active=(
                is_active(thread.get("status"))
                if isinstance(thread.get("status"), dict)
                and thread["status"].get("type") in {"active", "idle"}
                else None
            ),
            originator=_text(thread.get("originator")),
            source=_text(thread.get("source")),
        )


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _millis(seconds: Any) -> int:
    """A Codex timestamp, which counts seconds, as the milliseconds the protocol counts."""
    return int(seconds or 0) * 1000


def is_empty(summary: ThreadSummary) -> bool:
    """Whether the thread has nothing in it yet.

    A TUI opens a thread the moment it starts, before anything is typed into
    it, and that thread is indistinguishable from one the user opened and
    walked away from: no name, no preview, no rollout, and `thread/resume`
    refuses it. It becomes a session when it speaks.
    """
    return not summary.title and not summary.name


def summaries(threads: Any) -> list[ThreadSummary]:
    """Parse a `thread/list` page.

    Ephemeral and unreadable entries are dropped, and so is every thread
    another application on this machine owns: the index is the whole machine's
    history, and only what this device opened or a terminal started is a
    session here (A18).
    """
    found: list[ThreadSummary] = []
    for entry in threads if isinstance(threads, list) else []:
        if not isinstance(entry, dict) or is_ephemeral(entry):
            continue
        summary = ThreadSummary.parse(entry)
        if summary is not None and summary.ours:
            found.append(summary)
    return found
