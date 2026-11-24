"""JSON log formatter tests (`app/logging_config.py`)."""

from __future__ import annotations

import json
import logging

import pytest

from app.logging_config import JsonLogFormatter, request_id_var


def _make_record(**kwargs) -> logging.LogRecord:
    defaults = {
        "name": "relay.test",
        "level": logging.INFO,
        "pathname": __file__,
        "lineno": 1,
        "msg": "hello %s",
        "args": ("world",),
        "exc_info": None,
    }
    defaults.update(kwargs)
    return logging.LogRecord(**defaults)


def test_format_produces_valid_json_with_severity_message_timestamp():
    record = _make_record()
    line = JsonLogFormatter().format(record)
    payload = json.loads(line)
    assert payload["severity"] == "INFO"
    assert payload["message"] == "hello world"
    assert "timestamp" in payload
    assert payload["logger"] == "relay.test"


@pytest.mark.parametrize("request_id", ["req-abc-123", None])
def test_format_includes_request_id_only_when_set_in_context(request_id):
    token = request_id_var.set(request_id) if request_id else None
    try:
        payload = json.loads(JsonLogFormatter().format(_make_record()))
    finally:
        if token is not None:
            request_id_var.reset(token)
    if request_id:
        assert payload["requestId"] == request_id
    else:
        assert "requestId" not in payload


def test_format_includes_exception_traceback_when_present():
    try:
        raise ValueError("boom")
    except ValueError:
        import sys

        record = _make_record(msg="failed", args=(), exc_info=sys.exc_info())
    payload = json.loads(JsonLogFormatter().format(record))
    assert "ValueError" in payload["exception"]
    assert "boom" in payload["exception"]
