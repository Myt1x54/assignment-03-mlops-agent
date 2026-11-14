"""Structured JSON logging with secret redaction (DECISION D15).

Every line is one JSON object: ``ts``, ``level``, ``logger``, ``msg``, the current ``request_id``
(from a context variable set by the orchestrator) plus any ``extra=`` fields such as ``tool``,
``status``, ``duration_ms``.
Known secret values and common credential patterns are scrubbed from messages *and* extra fields.
"""

from __future__ import annotations

import contextvars
import json
import logging
import re
import sys
from datetime import UTC, datetime
from typing import Any

request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")

REDACTED = "***REDACTED***"
_PATTERNS = [
    re.compile(r"AIza[0-9A-Za-z_\-]{30,}"),  # Google API keys
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-]{16,}"),  # bearer tokens (JWTs)
    re.compile(r"eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"),  # bare JWTs
    re.compile(r"(?i)((?:password|passwd|secret|token|api[_-]?key)[\"']?\s*[:=]\s*[\"']?)[^\s\"',}]+"),
]
_known_secrets: set[str] = set()

_STANDARD_ATTRS = set(vars(logging.makeLogRecord({})).keys()) | {"message", "asctime"}


def register_secrets(*values: str) -> None:
    """Add concrete secret strings (e.g. from Settings) that must never be logged."""
    _known_secrets.update(v for v in values if v and len(v) >= 4)


def redact(text: str) -> str:
    for secret in sorted(_known_secrets, key=len, reverse=True):
        text = text.replace(secret, REDACTED)
    for pattern in _PATTERNS:
        text = pattern.sub(lambda m: (m.group(1) if m.groups() else "") + REDACTED, text)
    return text


def redact_obj(value: Any) -> Any:
    """Recursively redact strings inside dicts/lists (used for tool output and log extras)."""
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {k: redact_obj(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [redact_obj(v) for v in value]
    return value


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "request_id": getattr(record, "request_id", None) or request_id_var.get(),
            "msg": redact(record.getMessage()),
        }
        for key, value in record.__dict__.items():
            if key not in _STANDARD_ATTRS and key not in entry:
                entry[key] = redact_obj(value)
        if record.exc_info:
            entry["exc"] = redact(self.formatException(record.exc_info))
        return json.dumps(entry, default=str)


def setup_logging(level: str = "INFO", stream: Any = None) -> None:
    """Configure the root ``agent`` logger once (idempotent)."""
    logger = logging.getLogger("agent")
    logger.setLevel(level.upper())
    logger.propagate = False
    if not any(getattr(h, "_agent_json", False) for h in logger.handlers):
        handler = logging.StreamHandler(stream or sys.stderr)
        handler.setFormatter(JsonFormatter())
        handler._agent_json = True  # type: ignore[attr-defined]
        logger.addHandler(handler)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"agent.{name}")
