"""Structured JSON logging with correlation ids and redaction (FR-091).

One JSON object per line. Every record passes through the redaction
layer before it reaches a handler, and output is ASCII-escaped so an
emoji in a Telegram message can never raise an encoding error on a
legacy console (LIM-23).
"""

from __future__ import annotations

import contextvars
import json
import logging
import logging.handlers
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Optional

from hlbot.redact import redact, redact_text

correlation_id: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("correlation_id", default=None)

_RESERVED = frozenset(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {"message", "asctime", "taskName"}


def new_correlation_id(prefix: str = "") -> str:
    value = f"{prefix}{uuid.uuid4().hex[:12]}"
    correlation_id.set(value)
    return value


def set_correlation_id(value: Optional[str]) -> None:
    correlation_id.set(value)


def get_correlation_id() -> Optional[str]:
    return correlation_id.get()


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": redact_text(record.getMessage()),
        }
        corr = getattr(record, "corr", None) or correlation_id.get()
        if corr:
            payload["corr"] = corr
        for key, value in record.__dict__.items():
            if key not in _RESERVED and key != "corr" and not key.startswith("_"):
                payload[key] = redact(value, key.lower())
        if record.exc_info:
            payload["exc"] = redact_text(self.formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=True, default=str)


def setup_logging(log_path: Optional[Path], level: str = "INFO", *, console: bool = True) -> None:
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    formatter = JsonFormatter()
    if log_path is not None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            log_path, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
        )
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)
    if console:
        stream = logging.StreamHandler(sys.stderr)
        stream.setFormatter(formatter)
        root.addHandler(stream)
    # Third-party libraries log request details at DEBUG; keep them quiet.
    for noisy in ("telethon", "urllib3", "aiohttp.access", "hyperliquid", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
