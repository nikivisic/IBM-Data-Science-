"""Structured logging.

Every log line is a JSON object (or a readable console line when
`LOG_FORMAT=console`). Arbitrary context is attached per call site via the
`extra={"ctx": {...}}` convention, which keeps the message template stable and
the variable parts machine-parsable:

    log.info("ingest.token.done", extra={"ctx": {"mint": mint, "trades": 412}})
"""

from __future__ import annotations

import json
import logging
import sys
import time
from pathlib import Path
from typing import Any, Mapping, Optional

_RESERVED = frozenset(
    vars(logging.LogRecord("", 0, "", 0, "", (), None)).keys()
) | {"message", "asctime", "taskName"}


class JsonFormatter(logging.Formatter):
    """Render log records as single-line JSON."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
            + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
        }
        ctx = getattr(record, "ctx", None)
        if isinstance(ctx, Mapping):
            payload.update({str(k): _safe(v) for k, v in ctx.items()})
        # Anything passed through `extra=` that isn't a LogRecord internal.
        for key, value in record.__dict__.items():
            if key not in _RESERVED and key != "ctx":
                payload.setdefault(key, _safe(value))
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, separators=(",", ":"), default=str)


class ConsoleFormatter(logging.Formatter):
    """Compact human-readable rendering: `HH:MM:SS LEVEL event key=value`."""

    def format(self, record: logging.LogRecord) -> str:
        stamp = time.strftime("%H:%M:%S", time.localtime(record.created))
        line = f"{stamp} {record.levelname:<7} {record.getMessage()}"
        ctx = getattr(record, "ctx", None)
        if isinstance(ctx, Mapping) and ctx:
            line += "  " + " ".join(f"{k}={_safe(v)}" for k, v in ctx.items())
        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        return line


def _safe(value: Any) -> Any:
    if isinstance(value, float):
        return round(value, 6)
    if isinstance(value, (str, int, bool, type(None))):
        return value
    if isinstance(value, Path):
        return str(value)
    return str(value)


def configure_logging(
    level: str = "INFO",
    fmt: str = "json",
    log_file: Optional[Path] = None,
) -> None:
    """Install handlers on the root logger. Safe to call more than once."""
    formatter: logging.Formatter = (
        JsonFormatter() if fmt == "json" else ConsoleFormatter()
    )

    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)

    # Logs go to stderr so that CLI data output on stdout stays pipeable.
    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(formatter)
    root.addHandler(stream)

    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setFormatter(JsonFormatter())
        root.addHandler(file_handler)

    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    # urllib3 retry chatter is noise at our level of abstraction.
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
