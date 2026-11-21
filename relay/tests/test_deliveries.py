"""`app/deliveries.py`: the wrapper returns True only for a real `queued -> sent|failed`
transition."""

from __future__ import annotations

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


def test_mark_sent_only_once():
    msg_id = _msg()
    assert deliveries.mark_sent(msg_id, "b1") is True
    assert deliveries.mark_sent(msg_id, "b1") is False
    assert deliveries.mark_failed(msg_id, "b1", error="late") is False
    assert _state(msg_id) == "sent"


def test_mark_failed_only_once_and_stores_error():
    msg_id = _msg()
    assert deliveries.mark_failed(msg_id, "b1", error="sms_timeout") is True
    assert deliveries.mark_failed(msg_id, "b1", error="other") is False
    assert deliveries.mark_sent(msg_id, "b1") is False
    msg = messages_store.get_message(msg_id)
    assert msg is not None and msg.deliveries["b1"].error == "sms_timeout"


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
