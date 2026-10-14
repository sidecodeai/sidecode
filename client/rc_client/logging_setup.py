"""Structured stderr logging.

Never log tokens, pairing codes, prompt text or tool output: helpers here only
accept scalar context values and redact anything whose key looks secret.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from typing import Any

_SECRET_HINTS = ("token", "password", "secret", "pair", "code", "authorization", "key")


def _redact(key: str, value: Any) -> Any:
    lowered = key.lower()
    if any(hint in lowered for hint in _SECRET_HINTS):
        return "<redacted>"
    if isinstance(value, str) and len(value) > 200:
        return value[:200] + "…"
    return value


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": int(time.time() * 1000),
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": record.getMessage(),
        }
        extra = getattr(record, "context", None)
        if isinstance(extra, dict):
            for key, value in extra.items():
                payload[key] = _redact(key, value)
        if record.exc_info:
            payload["error"] = self.formatException(record.exc_info).splitlines()[-1]
        return json.dumps(payload, ensure_ascii=False)


class ContextLogger:
    """Thin adapter so call sites read `log.info("msg", key=value)`."""

    def __init__(self, name: str) -> None:
        self._log = logging.getLogger(name)

    def _emit(self, level: int, msg: str, exc_info: bool = False, **context: Any) -> None:
        self._log.log(level, msg, exc_info=exc_info, extra={"context": context})

    def debug(self, msg: str, **context: Any) -> None:
        self._emit(logging.DEBUG, msg, **context)

    def info(self, msg: str, **context: Any) -> None:
        self._emit(logging.INFO, msg, **context)

    def warning(self, msg: str, **context: Any) -> None:
        self._emit(logging.WARNING, msg, **context)

    def error(self, msg: str, **context: Any) -> None:
        self._emit(logging.ERROR, msg, **context)

    def exception(self, msg: str, **context: Any) -> None:
        self._emit(logging.ERROR, msg, exc_info=True, **context)


def logger(name: str) -> ContextLogger:
    return ContextLogger(name)


def setup_logging(level: str | None = None) -> None:
    """Install the JSON stderr handler once."""
    resolved = (level or os.environ.get("RC_LOG_LEVEL") or "info").upper()
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)
    root.setLevel(getattr(logging, resolved, logging.INFO))
    logging.getLogger("websockets").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
