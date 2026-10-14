"""Structured logging to stderr with fail-closed credential redaction.

Frame payloads are never logged at INFO: the gateway logs frame *types* and identifiers only. The
redaction pass is adapted from cc-remote's ``relay/log_safety.py`` (MIT).
"""

from __future__ import annotations

import copy
import json
import logging
import re
import sys
import time
from typing import Any

_SENSITIVE_KEYS = frozenset(
    {
        "token",
        "device_token",
        "password",
        "secret",
        "code",
        "authorization",
        "cookie",
        "set-cookie",
        "api_key",
        "endpoint",
        "p256dh",
        "auth",
        "email",
        "new_password",
        "current_password",
    }
)
_QUERY = re.compile(r"(?P<base>(?:[a-z][a-z0-9+.-]*://[^\s/?\"']+)?/[^\s?\"']*)\?[^\s\"']*", re.I)
_JSON_SECRET = re.compile(
    r"""(?i)(["'](?:token|password|secret|code|authorization|cookie|api_key)["']\s*:\s*["'])[^"']*(["'])"""
)
_NAMED_SECRET = re.compile(
    r"(?i)(\b(?:token|password|secret|authorization|cookie|set-cookie)\b\s*[:=]\s*)([^\s,;}]+)"
)
_BEARER = re.compile(r"(?i)\bbearer\s+[^\s,;}]+")
_PAIRING_CODE = re.compile(r"\bRC-[0-9A-Z]{4}-[0-9A-Z]{4}\b")
#: An email address anywhere in a line, which a log keeps only as its domain.
_EMAIL = re.compile(r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~.-]+@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+)")
_FILTER_NAME = "rc_gateway_redaction"


def redact_text(value: str) -> str:
    """Strip query strings, bearer tokens, pairing codes and named secrets from one string."""
    redacted = _QUERY.sub(r"\g<base>?[redacted]", value)
    redacted = _JSON_SECRET.sub(r"\1[redacted]\2", redacted)
    redacted = _BEARER.sub("Bearer [redacted]", redacted)
    redacted = _PAIRING_CODE.sub("RC-[redacted]", redacted)
    redacted = _EMAIL.sub(r"[redacted]@\1", redacted)
    return _NAMED_SECRET.sub(r"\1[redacted]", redacted)


def redact_value(value: Any) -> Any:
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, bytes):
        return redact_text(value.decode("utf-8", "replace")).encode("utf-8")
    if isinstance(value, tuple):
        return tuple(redact_value(item) for item in value)
    if isinstance(value, list):
        return [redact_value(item) for item in value]
    if isinstance(value, dict):
        return {
            key: ("[redacted]" if str(key).lower() in _SENSITIVE_KEYS else redact_value(item))
            for key, item in value.items()
        }
    return value


class RedactionFilter(logging.Filter):
    """Redact before any handler formats a record; never crash the gateway while logging."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            record.msg = redact_value(record.msg)
            record.args = redact_value(record.args)
            fields = getattr(record, "fields", None)
            if fields is not None:
                record.fields = redact_value(fields)
            if hasattr(record, "scope"):
                record.scope = redact_value(record.scope)
        except Exception:
            record.msg = "log record redacted"
            record.args = ()
            record.fields = {}
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": int(record.created * 1000),
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": record.getMessage(),
        }
        fields = getattr(record, "fields", None)
        if isinstance(fields, dict):
            payload.update(fields)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)


class Logger:
    """Thin wrapper that turns keyword arguments into structured fields."""

    def __init__(self, name: str) -> None:
        self._log = logging.getLogger(name)

    def _emit(self, level: int, message: str, **fields: Any) -> None:
        self._log.log(level, message, extra={"fields": fields})

    def debug(self, message: str, **fields: Any) -> None:
        self._emit(logging.DEBUG, message, **fields)

    def info(self, message: str, **fields: Any) -> None:
        self._emit(logging.INFO, message, **fields)

    def warning(self, message: str, **fields: Any) -> None:
        self._emit(logging.WARNING, message, **fields)

    def error(self, message: str, **fields: Any) -> None:
        self._emit(logging.ERROR, message, **fields)

    def exception(self, message: str, **fields: Any) -> None:
        self._log.exception(message, extra={"fields": fields})


def logger(name: str) -> Logger:
    return Logger(name)


def configure_logging(level: str = "info") -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(RedactionFilter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    logging.Formatter.converter = time.gmtime


def uvicorn_log_config(level: str = "info") -> dict[str, Any]:
    """Uvicorn's logging config with the same JSON formatter and redaction filter."""
    from uvicorn.config import LOGGING_CONFIG

    config = copy.deepcopy(LOGGING_CONFIG)
    config.setdefault("filters", {})[_FILTER_NAME] = {"()": RedactionFilter}
    for formatter in config.get("formatters", {}).values():
        formatter.clear()
        formatter["()"] = JsonFormatter
    for handler in config.get("handlers", {}).values():
        handler["filters"] = [*(handler.get("filters") or []), _FILTER_NAME]
    for entry in config.get("loggers", {}).values():
        entry["level"] = level.upper()
    return config
