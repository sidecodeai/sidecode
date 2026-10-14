"""Git status for `device.git` and isolated worktree creation for new sessions."""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
from pathlib import Path
from typing import Any

from .errors import RcError

GIT_TIMEOUT = 8.0
_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _git_env() -> dict[str, str]:
    """Neutralise config a checked-out repository could use to run commands."""
    env = dict(os.environ)
    env.update(
        {
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_OPTIONAL_LOCKS": "0",
        }
    )
    return env


async def _git(cwd: str, *args: str, timeout: float = GIT_TIMEOUT) -> tuple[int, str, str]:
    process = await asyncio.create_subprocess_exec(
        "git",
        "-c",
        "core.fsmonitor=false",
        "-c",
        "core.pager=cat",
        "-c",
        "core.hooksPath=" + os.devnull,
        *args,
        cwd=cwd,
        env=_git_env(),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except TimeoutError:
        # The child may have exited between the timeout and the signal.
        with contextlib.suppress(ProcessLookupError):
            process.kill()
            await process.wait()
        return 124, "", "git timed out"
    return process.returncode or 0, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")


async def git_info(path: str) -> dict[str, Any]:
    """Branch / dirty / ahead / behind for `path`, or `{"is_repo": false}`."""
    target = Path(path).expanduser()
    if not target.is_dir():
        return {"is_repo": False}
    code, out, _ = await _git(str(target), "rev-parse", "--is-inside-work-tree")
    if code != 0 or out.strip() != "true":
        return {"is_repo": False}

    _, branch_out, _ = await _git(str(target), "rev-parse", "--abbrev-ref", "HEAD")
    branch = branch_out.strip() or None
    _, status_out, _ = await _git(str(target), "status", "--porcelain")
    dirty = bool(status_out.strip())
    ahead = behind = 0
    code, counts, _ = await _git(
        str(target), "rev-list", "--left-right", "--count", "@{upstream}...HEAD"
    )
    if code == 0:
        parts = counts.split()
        if len(parts) == 2:
            behind, ahead = int(parts[0]), int(parts[1])
    return {"is_repo": True, "branch": branch, "dirty": dirty, "ahead": ahead, "behind": behind}


async def session_git(path: str, worktree: bool = False) -> dict[str, Any] | None:
    """The `Session.git` summary, or None when `path` is not a repository."""
    info = await git_info(path)
    if not info.get("is_repo"):
        return None
    return {
        "branch": info.get("branch"),
        "dirty": bool(info.get("dirty")),
        "ahead": int(info.get("ahead") or 0),
        "behind": int(info.get("behind") or 0),
        "worktree": worktree,
    }


def slugify(value: str) -> str:
    slug = _SLUG_RE.sub("-", value.lower()).strip("-")
    return (slug or "session")[:32]


async def create_worktree(repo: str, slug: str) -> str:
    """Add `<repo>/.rc-worktrees/<slug>` on a fresh branch and return its path."""
    info = await git_info(repo)
    if not info.get("is_repo"):
        raise RcError("bad_request", f"worktree requested but {repo} is not a git repository")
    code, root, _ = await _git(repo, "rev-parse", "--show-toplevel")
    if code != 0:
        raise RcError("internal", "cannot locate the repository root")
    base = Path(root.strip())
    target = base / ".rc-worktrees" / slug
    branch = f"rc/{slug}"
    suffix = 1
    while target.exists():
        suffix += 1
        target = base / ".rc-worktrees" / f"{slug}-{suffix}"
        branch = f"rc/{slug}-{suffix}"
    code, _, err = await _git(str(base), "worktree", "add", "-b", branch, str(target), timeout=60.0)
    if code != 0:
        raise RcError("internal", f"git worktree add failed: {err.strip()[:200]}")
    return str(target)
