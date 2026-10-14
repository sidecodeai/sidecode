"""`device.dirs` and `device.mkdir`: a browsable, directory-only view of the device."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .errors import RcError

MAX_ENTRIES = 400
NAME_MAX_BYTES = 255


def _is_git_repo(path: Path) -> bool:
    return (path / ".git").exists()


def list_dirs(path: str | None, recents: list[tuple[str, int]]) -> dict[str, Any]:
    """List sub-directories of `path` (home when omitted), hidden ones excluded."""
    target = Path(path).expanduser() if path else Path.home()
    try:
        target = target.resolve()
    except OSError as exc:
        raise RcError("not_found", f"cannot resolve {target}") from exc
    if not target.is_dir():
        raise RcError("not_found", f"not a directory: {target}")

    entries: list[dict[str, Any]] = []
    try:
        with_names = sorted(target.iterdir(), key=lambda item: item.name.lower())
    except PermissionError as exc:
        raise RcError("forbidden", f"permission denied: {target}") from exc
    for child in with_names:
        if child.name.startswith("."):
            continue
        try:
            if not child.is_dir():
                continue
        except OSError:
            continue
        entries.append({"name": child.name, "path": str(child), "is_git": _is_git_repo(child)})
        if len(entries) >= MAX_ENTRIES:
            break

    parent = str(target.parent) if target.parent != target else None
    return {
        "path": str(target),
        "parent": parent,
        "entries": entries,
        "recent": [{"path": item, "last_used": used} for item, used in recents],
    }


def _check_name(name: str) -> None:
    """A37: one path component a person can type, and nothing else."""
    if not name:
        raise RcError("bad_request", "a folder name is required")
    if "/" in name or "\\" in name:
        raise RcError("bad_request", "a folder name cannot contain a slash")
    if "\0" in name:
        raise RcError("bad_request", "a folder name cannot contain a null byte")
    if name.startswith("."):
        raise RcError("bad_request", "a folder name cannot start with a dot")
    if len(name.encode("utf-8")) > NAME_MAX_BYTES:
        raise RcError("bad_request", f"a folder name can be at most {NAME_MAX_BYTES} bytes")


def _resolve_parent(path: str) -> Path:
    """The directory the folder is made in: absolute, existing, resolved before joining."""
    if not path or not Path(path).is_absolute():
        raise RcError("bad_request", "path must be an absolute directory path")
    try:
        parent = Path(path).resolve()
    except OSError as exc:
        raise RcError("not_found", f"cannot resolve {path}") from exc
    if not parent.is_dir():
        raise RcError("not_found", f"not a directory: {parent}")
    return parent


def make_dir(path: str, name: str, recents: list[tuple[str, int]]) -> dict[str, Any]:
    """Make one directory `name` inside `path` and reply with its listing (A37)."""
    _check_name(name)
    parent = _resolve_parent(path)
    target = parent / name
    try:
        target.mkdir()
    except FileExistsError as exc:
        raise RcError("conflict", f"a folder with that name already exists: {target}") from exc
    except PermissionError as exc:
        raise RcError("forbidden", f"permission denied: {parent}") from exc
    except OSError as exc:
        raise RcError("internal", f"cannot create {target}: {exc.strerror or exc}") from exc
    return list_dirs(str(target), recents)


def merge_recents(*sources: list[tuple[str, int]]) -> list[tuple[str, int]]:
    """Collapse recent-directory candidates, newest use wins, capped at 20."""
    best: dict[str, int] = {}
    for source in sources:
        for path, used in source:
            if not path:
                continue
            if used > best.get(path, 0):
                best[path] = used
    ordered = sorted(best.items(), key=lambda item: item[1], reverse=True)
    return ordered[:20]
