"""Incremental JSONL tailing shared by both mirroring backends.

Growth is measured with `st_size`, never `st_mtime`: resuming an agent session
touches mtime without appending a byte. The file's `(device, inode)` pair is
tracked as well so a rotated or replaced file restarts from the beginning
instead of silently skipping its content.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any

READ_CHUNK = 4 * 1024 * 1024


@dataclass(slots=True)
class FileTail:
    path: str
    offset: int = 0
    identity: tuple[int, int] | None = field(default=None)
    skipped_records: int = 0

    def stat(self) -> os.stat_result | None:
        try:
            return os.stat(self.path)
        except OSError:
            return None

    def size(self) -> int:
        stat = self.stat()
        return stat.st_size if stat else 0

    def seek_to_end(self) -> None:
        stat = self.stat()
        if stat is None:
            return
        self.offset = stat.st_size
        self.identity = (stat.st_dev, st_ino(stat))

    def read_new(self) -> list[dict[str, Any]]:
        """Return the JSON rows appended since the previous read."""
        stat = self.stat()
        if stat is None:
            return []
        identity = (stat.st_dev, st_ino(stat))
        if self.identity is not None and identity != self.identity:
            self.offset = 0
        self.identity = identity
        if stat.st_size < self.offset:
            self.offset = 0
        if stat.st_size == self.offset:
            return []
        try:
            with open(self.path, "rb") as handle:
                handle.seek(self.offset)
                data = handle.read(min(READ_CHUNK, stat.st_size - self.offset))
        except OSError:
            return []
        consumed = data.rfind(b"\n")
        if consumed == -1:
            # One record longer than a chunk would otherwise be re-read forever.
            # Skip past it: a single lost row beats a permanently stuck mirror.
            if stat.st_size - self.offset > READ_CHUNK:
                self.offset += len(data)
                self.skipped_records += 1
            return []
        self.offset += consumed + 1
        rows: list[dict[str, Any]] = []
        for line in data[: consumed + 1].splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                rows.append(row)
        return rows


def st_ino(stat: os.stat_result) -> int:
    return int(stat.st_ino)
