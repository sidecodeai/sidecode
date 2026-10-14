"""Plain text for the Codex commands that only report (amendment A27).

A terminal draws these as panels of its own; an app has one `tool_call` block
to draw them in, so every helper here turns one Codex payload into the lines
that block carries. Pure functions, so the tests compare text against payloads
recorded from the real daemon rather than running one.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

# Enough of a long list to be useful without burying the block; the daemon is
# the only thing that could make these lists much longer than a screen.
MAX_ROWS = 200
MAX_DESCRIPTION = 120

_SANDBOX_LABELS = {
    "readOnly": "read-only",
    "workspaceWrite": "workspace-write",
    "dangerFullAccess": "danger-full-access",
    "externalSandbox": "external-sandbox",
}


@dataclass(slots=True)
class ThreadFacts:
    """What a session already knows about its thread, without asking again.

    Every field but `cwd` comes back from `thread/start` and `thread/resume`
    and is kept current by `thread/settings/updated`, so `/status` costs no
    round trip; `usage` is the last `thread/tokenUsage/updated` seen.
    """

    cwd: str
    model: str | None = None
    effort: str | None = None
    speed: str | None = None
    permission_mode: str = "on-request"
    sandbox: dict[str, Any] | None = None
    usage: dict[str, Any] = field(default_factory=dict)


def _clip(text: str, limit: int = MAX_DESCRIPTION) -> str:
    flat = " ".join(str(text or "").split())
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"


def _count(total: int, singular: str, plural: str | None = None) -> str:
    word = singular if total == 1 else (plural or f"{singular}s")
    return f"{total:,} {word}"


def _rows(entries: list[str], header: str, empty: str) -> str:
    if not entries:
        return empty
    shown = entries[:MAX_ROWS]
    lines = [header, *(f"  {row}" for row in shown)]
    if len(entries) > len(shown):
        lines.append(f"  … and {len(entries) - len(shown):,} more")
    return "\n".join(lines)


def sandbox_label(sandbox: Any) -> str:
    """The sandbox a thread runs under, in the words the terminal uses."""
    if not isinstance(sandbox, dict):
        return "unknown"
    kind = str(sandbox.get("type") or "")
    label = _SANDBOX_LABELS.get(kind, kind or "unknown")
    if kind == "workspaceWrite":
        roots = [str(root) for root in (sandbox.get("writableRoots") or []) if root]
        if sandbox.get("networkAccess"):
            label += ", network access"
        if roots:
            label += f", also writable: {', '.join(roots[:4])}"
    return label


def status_text(facts: ThreadFacts, name: str | None = None) -> str:
    """What `/status` prints: how this session is configured, and its token use."""
    lines = [f"Model: {facts.model or 'unknown'}"]
    if facts.effort:
        lines.append(f"Reasoning: {facts.effort}")
    lines.append(f"Speed: {facts.speed or 'standard'}")
    lines.append(f"Approvals: {facts.permission_mode}")
    lines.append(f"Sandbox: {sandbox_label(facts.sandbox)}")
    lines.append(f"Directory: {facts.cwd}")
    if name:
        lines.append(f"Thread: {name}")
    lines.append(f"Tokens: {tokens_line(facts.usage)}")
    return "\n".join(lines)


def tokens_line(usage: dict[str, Any]) -> str:
    """The token line of `/status`, from the last usage the runner saw."""
    total = int(usage.get("total_tokens") or 0)
    window = usage.get("context_window")
    if not total:
        return "none used yet"
    if isinstance(window, int) and window > 0:
        return f"{total:,} of {window:,} used ({round(total * 100 / window)}%)"
    return f"{total:,} used"


def _window_label(minutes: Any) -> str:
    if not isinstance(minutes, int) or minutes <= 0:
        return "Limit"
    if minutes == 10080:
        return "Weekly limit"
    if minutes == 1440:
        return "Daily limit"
    if minutes % 60 == 0:
        return f"{minutes // 60}-hour limit"
    return f"{minutes}-minute limit"


def _resets(at: Any) -> str:
    if not isinstance(at, int) or at <= 0:
        return ""
    return f", resets {time.strftime('%d %b %H:%M', time.localtime(at))}"


def _window_line(window: Any, minutes_key: str = "windowDurationMins") -> str | None:
    if not isinstance(window, dict):
        return None
    used = window.get("usedPercent")
    if not isinstance(used, int):
        return None
    label = _window_label(window.get(minutes_key))
    return f"{label}: {used}% used{_resets(window.get('resetsAt'))}"


def usage_text(usage: dict[str, Any], limits: dict[str, Any]) -> str:
    """What `/usage` prints: the account's windows, then its lifetime totals."""
    raw = limits.get("rateLimits")
    snapshot: dict[str, Any] = raw if isinstance(raw, dict) else {}
    lines: list[str] = []
    plan = snapshot.get("planType")
    if isinstance(plan, str) and plan:
        lines.append(f"Plan: {plan}")
    for key in ("primary", "secondary"):
        line = _window_line(snapshot.get(key))
        if line:
            lines.append(line)
    credits = snapshot.get("credits")
    if isinstance(credits, dict):
        if credits.get("unlimited"):
            lines.append("Credits: unlimited")
        elif credits.get("hasCredits"):
            lines.append(f"Credits: {credits.get('balance') or '0'}")
    resets = limits.get("rateLimitResetCredits")
    if isinstance(resets, dict) and int(resets.get("availableCount") or 0) > 0:
        lines.append(f"Limit resets available: {int(resets['availableCount']):,}")
    reported = usage.get("summary")
    summary: dict[str, Any] = reported if isinstance(reported, dict) else {}
    for label, key in (
        ("Lifetime tokens", "lifetimeTokens"),
        ("Busiest day", "peakDailyTokens"),
    ):
        value = summary.get(key)
        if isinstance(value, int) and value > 0:
            lines.append(f"{label}: {value:,}")
    streak = summary.get("currentStreakDays")
    if isinstance(streak, int) and streak > 0:
        lines.append(f"Current streak: {_count(streak, 'day')}")
    return "\n".join(lines) or "no account usage reported"


def _flatten(data: Any, key: str) -> list[dict[str, Any]]:
    """The per-directory answers of `skills/list` and `hooks/list`, in one list."""
    collected: list[dict[str, Any]] = []
    for group in data if isinstance(data, list) else []:
        if not isinstance(group, dict):
            continue
        for entry in group.get(key) or []:
            if isinstance(entry, dict):
                collected.append(entry)
    return collected


def _errors(data: Any) -> list[str]:
    problems: list[str] = []
    for group in data if isinstance(data, list) else []:
        if not isinstance(group, dict):
            continue
        for entry in group.get("errors") or []:
            if isinstance(entry, dict) and entry.get("message"):
                problems.append(f"{entry.get('path') or '?'}: {_clip(entry['message'])}")
    return problems


def skills_text(data: Any) -> str:
    """What `/skills` prints: one line per skill Codex found for this directory."""
    skills = _flatten(data, "skills")
    rows: list[str] = []
    for skill in skills:
        name = str(skill.get("name") or "")
        if not name:
            continue
        about = _clip(skill.get("shortDescription") or skill.get("description") or "")
        scope = str(skill.get("scope") or "")
        suffix = "" if skill.get("enabled", True) else " (disabled)"
        row = f"{name}{suffix}"
        if scope:
            row += f" [{scope}]"
        rows.append(f"{row} — {about}" if about else row)
    text = _rows(rows, f"{_count(len(rows), 'skill')} available", "no skills are available here")
    problems = _errors(data)
    if problems:
        text += "\n" + _rows(problems, "Problems", "")
    return text


def _hook_handler(hook: dict[str, Any]) -> str:
    handler = str(hook.get("handlerType") or "")
    if handler == "command":
        return f"command {_clip(hook.get('command') or '', 80)}"
    if handler == "mcpTool":
        return f"mcp {hook.get('server') or '?'}.{hook.get('tool') or '?'}"
    return handler or "hook"


def hooks_text(data: Any) -> str:
    """What `/hooks` prints: the lifecycle hooks that apply to this directory."""
    hooks = _flatten(data, "hooks")
    rows: list[str] = []
    for hook in hooks:
        event = str(hook.get("eventName") or "?")
        notes = []
        matcher = hook.get("matcher")
        if isinstance(matcher, str) and matcher:
            notes.append(f"matches {matcher}")
        trust = str(hook.get("trustStatus") or "")
        if trust and trust != "trusted":
            notes.append(trust)
        if not hook.get("enabled", True):
            notes.append("disabled")
        source = str(hook.get("source") or "")
        if source:
            notes.append(source)
        tail = f" ({', '.join(notes)})" if notes else ""
        rows.append(f"{event} — {_hook_handler(hook)}{tail}")
    text = _rows(rows, f"{_count(len(rows), 'hook')} configured", "no hooks are configured here")
    problems = _errors(data)
    if problems:
        text += "\n" + _rows(problems, "Problems", "")
    return text


def mcp_text(data: Any) -> str:
    """What `/mcp` prints: each configured MCP server, its state and tool count."""
    rows: list[str] = []
    for server in data if isinstance(data, list) else []:
        if not isinstance(server, dict):
            continue
        name = str(server.get("name") or "")
        if not name:
            continue
        tools = server.get("tools")
        count = len(tools) if isinstance(tools, dict) else 0
        state = str(server.get("runtimeStatus") or "") or "not started"
        row = f"{name} — {state}, {_count(count, 'tool')}"
        auth = str(server.get("authStatus") or "")
        if auth in {"notLoggedIn", "authenticationRequired"}:
            row += ", not signed in"
        error = server.get("toolsError")
        if isinstance(error, str) and error:
            row += f" — {_clip(error, 80)}"
        rows.append(row)
    return _rows(rows, f"{_count(len(rows), 'MCP server')}", "no MCP servers are configured")


def diff_text(stat: str, patch: str, untracked: list[str]) -> str:
    """What `/diff` prints: the summary, the patch, then the untracked paths."""
    parts: list[str] = []
    if stat.strip():
        parts.append(stat.rstrip())
    if patch.strip():
        parts.append(patch.rstrip())
    if untracked:
        header = f"Untracked ({len(untracked):,}):"
        parts.append(_rows(untracked, header, header))
    return "\n\n".join(parts) if parts else "the working tree is clean"


def diff_summary(stat: str, untracked: list[str]) -> str:
    """The one-line summary beside the `/diff` block."""
    last = [line.strip() for line in stat.strip().splitlines() if line.strip()]
    head = last[-1] if last else ""
    if untracked:
        extra = f"{len(untracked):,} untracked"
        return f"{head}, {extra}" if head else extra
    return head or "no changes"
