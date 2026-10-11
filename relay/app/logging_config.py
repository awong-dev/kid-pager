"""Structured JSON logging for Cloud Logging, with a per-request correlation id."""

from __future__ import annotations

import contextvars
import json
import logging
import time

request_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "request_id", default=None
)

_LEVEL_TO_SEVERITY: dict[int, str] = {
    logging.DEBUG: "DEBUG",
    logging.INFO: "INFO",
    logging.WARNING: "WARNING",
    logging.ERROR: "ERROR",
    logging.CRITICAL: "CRITICAL",
}


class JsonLogFormatter(logging.Formatter):
    """One JSON object per line; `requestId` is present when a request is in flight."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "severity": _LEVEL_TO_SEVERITY.get(record.levelno, record.levelname),
            "message": record.getMessage(),
            "timestamp": (
                time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
                + f".{int(record.msecs):03d}Z"
            ),
            "logger": record.name,
        }
        request_id = request_id_var.get()
        if request_id is not None:
            payload["requestId"] = request_id
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        # default=str: a log call must never raise on a non-JSON value.
        return json.dumps(payload, default=str)


def configure_logging(level: str | int = "INFO") -> None:
    """Replace the root logger's handlers with one JSON handler. Idempotent."""
    root = logging.getLogger()
    root.setLevel(level)
    handler = logging.StreamHandler()
    handler.setFormatter(JsonLogFormatter())
    root.handlers = [handler]
    # httpx logs request URLs at INFO; the Soracom enrollment URL carries the full IMSI.
    logging.getLogger("httpx").setLevel(logging.WARNING)
