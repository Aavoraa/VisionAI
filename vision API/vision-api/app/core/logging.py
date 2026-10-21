"""Structured JSON logging with secret/PII redaction."""

import json
import logging
import re
import sys
import traceback
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

request_id_ctx: ContextVar[str | None] = ContextVar("request_id", default=None)

REDACTED = "[REDACTED]"

_SENSITIVE_KEY_RE = re.compile(
    # token(?!s): redact "token"/"access_token" but keep usage counts such as "input_tokens".
    r"(authorization|api[_-]?key|password|passwd|secret|token(?!s)|cookie|pepper|database_url|dsn|credential)",
    re.IGNORECASE,
)
_SCRUB_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"sk_live_[A-Za-z0-9_\-]{8,}"), REDACTED),
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-~+/]+=*"), r"\1" + REDACTED),
    (re.compile(r"(?<=://)[^/@\s:]+:[^/@\s]+@"), REDACTED + "@"),
)

# Attributes present on every LogRecord; anything else was passed via ``extra=``.
_STANDARD_ATTRS = frozenset(
    logging.LogRecord("", 0, "", 0, "", (), None).__dict__.keys()
    | {"message", "asctime", "taskName", "color_message"}
)


def scrub(text: str) -> str:
    for pattern, replacement in _SCRUB_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def redact(value: Any, key: str | None = None) -> Any:
    if key is not None and _SENSITIVE_KEY_RE.search(key):
        return REDACTED
    if isinstance(value, str):
        return scrub(value)
    if isinstance(value, dict):
        return {str(k): redact(v, str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [redact(v) for v in value]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return scrub(str(value))


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": scrub(record.getMessage()),
        }
        request_id = request_id_ctx.get()
        if request_id is not None:
            payload["request_id"] = request_id
        for key, value in record.__dict__.items():
            if key not in _STANDARD_ATTRS and not key.startswith("_"):
                payload[key] = redact(value, key)
        if record.exc_info and record.exc_info[0] is not None:
            exc_type, exc_value, exc_tb = record.exc_info
            payload["exc_type"] = exc_type.__name__
            payload["exc"] = scrub("".join(traceback.format_exception(exc_type, exc_value, exc_tb)))
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(level: str) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)

    # Uvicorn's access log prints raw query strings; the app emits its own sanitised access log.
    for name in ("uvicorn", "uvicorn.error"):
        uv_logger = logging.getLogger(name)
        uv_logger.handlers.clear()
        uv_logger.propagate = True
    access = logging.getLogger("uvicorn.access")
    access.handlers.clear()
    access.propagate = False
    access.disabled = True

    # httpx/httpcore log full URLs at INFO; user-supplied image URLs may be presigned
    # (signature in the query string), so only warnings and above are emitted.
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)
