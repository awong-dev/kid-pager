"""`app/deliveries.py`: the wrapper returns True only for a real `queued -> sent|failed`
transition."""

from __future__ import annotations

import pytest

from app import deliveries
from app.store import messages as messages_store


def _msg(wire_id: str = "u_1") -> str:
    msg = messages_store.create_message(
        sender_uid="alice",
        recipient_uid="bob",
        kind="text",
        ts=1000,
        body="hi",
        wire_id=wire_id,
        deliveries={"b1": {"kind": "sms", "state": "queued", "attempts": 0}},
    )
    assert msg is not None
    return msg.id


def _state(msg_id: str) -> str:
    msg = messages_store.get_message(msg_id)
    assert msg is not None
    return msg.deliveries["b1"].state


@pytest.mark.parametrize(
    ("first", "second", "state", "error"),
    [
        pytest.param("mark_sent", "mark_failed", "sent", None, id="sent_first"),
        pytest.param("mark_failed", "mark_sent", "failed", "sms_timeout", id="failed_first"),
    ],
)
def test_a_delivery_leaves_queued_only_once(first: str, second: str, state: str, error: str | None):
    msg_id = _msg()

    def call(name: str, error: str) -> bool:
        kwargs = {"error": error} if name == "mark_failed" else {}
        return getattr(deliveries, name)(msg_id, "b1", **kwargs)

    assert call(first, "sms_timeout") is True
    assert call(first, "other") is False
    assert call(second, "late") is False
    assert _state(msg_id) == state
    msg = messages_store.get_message(msg_id)
    assert msg is not None and msg.deliveries["b1"].error == error


def test_unknown_message_or_delivery_is_false():
    assert deliveries.mark_sent("nope", "b1") is False
    assert deliveries.mark_failed("nope", "b1") is False
    msg_id = _msg()
    assert deliveries.mark_sent(msg_id, "zz") is False


def test_record_attempt_flips_to_failed_once():
    msg_id = _msg()
    assert (
        deliveries.record_attempt(msg_id, "b1", ok=False, error="x", max_attempts=2)
        is False
    )
    assert (
        deliveries.record_attempt(msg_id, "b1", ok=False, error="x", max_attempts=2)
        is True
    )
    assert _state(msg_id) == "failed"
    assert (
        deliveries.record_attempt(msg_id, "b1", ok=False, error="x", max_attempts=2)
        is False
    )
