"""Put the shim directory in front of PATH in the user's shell startup file.

The block is delimited by markers and edited by append or by rewrite of those
markers only. Shell startup files are frequently symlinks into a dotfile
repository, so the file is opened in place and never replaced.
"""

from __future__ import annotations

import os
from pathlib import Path

from . import paths

BEGIN = "# >>> remote-control >>>"
END = "# <<< remote-control <<<"

_RC_BY_SHELL = {"zsh": ".zshrc", "bash": ".bashrc", "ksh": ".kshrc"}


def rc_file(shell: str | None = None, home: Path | None = None) -> Path:
    """The startup file for the login shell, defaulting to the POSIX profile."""
    name = os.path.basename(shell or os.environ.get("SHELL") or "")
    return (home or Path.home()) / _RC_BY_SHELL.get(name, ".profile")


def block() -> str:
    return f'{BEGIN}\nexport PATH="{paths.bin_dir()}:$PATH"\n{END}\n'


def _split(text: str) -> tuple[str, str] | None:
    """The text before and after our block, or None when it is absent."""
    start = text.find(BEGIN)
    if start < 0:
        return None
    end = text.find(END, start)
    if end < 0:
        return text[:start], ""
    tail = text[end + len(END) :]
    return text[:start], tail.lstrip("\n")


def add(path: Path) -> bool:
    """Append the block, or refresh it in place. True when the file changed."""
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    parts = _split(text)
    if parts is None:
        separator = "" if not text or text.endswith("\n") else "\n"
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"{separator}\n{block()}")
        return True
    head, tail = parts
    updated = head + block() + tail
    if updated == text:
        return False
    _write_in_place(path, updated)
    return True


def installed(path: Path) -> bool:
    """True when the startup file already carries our block."""
    if not path.exists():
        return False
    return _split(path.read_text(encoding="utf-8")) is not None


def remove(path: Path) -> bool:
    """Strip the block by its markers. True when the file changed."""
    if not path.exists():
        return False
    text = path.read_text(encoding="utf-8")
    parts = _split(text)
    if parts is None:
        return False
    head, tail = parts
    updated = head.rstrip("\n") + ("\n" if head.strip() else "")
    updated = updated + tail
    _write_in_place(path, updated)
    return True


def _write_in_place(path: Path, text: str) -> None:
    """Truncate and rewrite the existing inode, so a symlink stays a symlink."""
    with path.open("w", encoding="utf-8") as handle:
        handle.write(text)
