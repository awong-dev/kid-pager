"""Delivery receipts for pages sent via a bridge phone or peer relay (PROTOCOL.md §3.2 `rcpt`)."""

from __future__ import annotations

import logging
import time
from typing import Any, Literal

from app import booknames, clockfmt, sysmsg
from app.broker import BrokerClient
from app.store import devices as devices_store
from app.store import messages as messages_store
from app.store import users as users_store

logger = logging.getLogger("relay.receipts")

# Backend kinds acknowledged out-of-band.
RECEIPT_KINDS = frozenset({"sms", "bridge", "fed"})

# PROTOCOL.md §3.2: `rsn` <= 16 bytes.
RSN_MAX_BYTES = 16
NOTICE_REASON_MAX_BYTES = 20
# Ceiling the recipient name is cut to fit.
NOTICE_BODY_MAX_BYTES = 90

# `rsn` of the receipt a server frozen for a move answers a text with.
MOVING_REASON = "moving"

Outcome = Literal["system", "rcpt", "skip"]

def _origin_devices(msg: messages_store.Message) -> list[devices_store.Device]:
    """The sender's pager; falls back to its non-revoked devices when `originDeviceId` is absent."""
    if msg.originBackendKind != "pager" or not msg.wireId:
        return []
    if msg.originDeviceId:
        device = devices_store.get_device(msg.originDeviceId)
        return [device] if device is not None and device.revokedAt is None else []
    return [d for d in devices_store.list_devices(msg.senderUid) if d.revokedAt is None]


def _base_wire_id(wire_id: str) -> str:
    """The wire id the pager sent: a split part `<wire>~<i>` stands for `<wire>`."""
    return wire_id.split("~", 1)[0]


def _split_gate(msg: messages_store.Message, bid: str, st: str) -> bool:
    """Whether a terminal `st` of one split part is reported: sent only when all parts are."""
    for part in messages_store.list_split_siblings(msg):
        d = part.deliveries.get(bid)
        if d is None or d.kind not in RECEIPT_KINDS:
            continue
        if st == "sent" and d.state != "sent":
            return False
        if st == "failed" and part.id != msg.id and d.state == "failed":
            return False
    return True


def _recipient_name(msg: messages_store.Message) -> str:
    group = None
    if msg.groupMsgId is not None:
        group = messages_store.get_conversation(msg.convKey)
    return booknames.book_name(msg.senderUid, msg.recipientUid, group=group)


def on_delivery_terminal(
    msg_id: str,
    bid: str,
    st: Literal["sent", "failed"],
    *,
    broker: BrokerClient | None = None,
) -> Outcome:
    """Return which path ran (`skip`: nothing sent). Never raises."""
    try:
        return _on_delivery_terminal(msg_id, bid, st, broker or sysmsg.get_broker())
    except Exception:
        logger.exception("receipt msg=%s bid=%s st=%s failed", msg_id, bid, st)
        return "skip"


def _on_delivery_terminal(
    msg_id: str, bid: str, st: Literal["sent", "failed"], broker: BrokerClient | None
) -> Outcome:
    msg = messages_store.get_message(msg_id)
    if msg is None:
        return "skip"
    delivery = msg.deliveries.get(bid)
    if delivery is None or delivery.kind not in RECEIPT_KINDS:
        return "skip"
    devices = _origin_devices(msg)
    if not devices:
        return "skip"
    if broker is None:
        logger.warning(
            "receipt msg=%s bid=%s st=%s: no broker client; not sent", msg_id, bid, st
        )
        return "skip"
    assert msg.wireId is not None
    ref = _base_wire_id(msg.wireId)
    if not _split_gate(msg, bid, st):
        return "skip"
    reason = delivery.error or "failed"

    outcome: Outcome = "skip"
    for device in devices:
        mid = sysmsg.derived_id("rcpt", device.id, ref, bid, st)
        if device.status.mb is not None:
            obj: dict[str, Any] = {
                "v": 1,
                "id": mid,
                "ts": int(time.time()),
                "kind": "rcpt",
                "ref": ref,
                "st": st,
            }
            if st == "failed":
                obj["rsn"] = sysmsg.clip_bytes(reason, RSN_MAX_BYTES)
            obj["ack"] = None
            ok = broker.publish_down(device.id, obj)
            path: Outcome = "rcpt"
        elif st == "failed":
            body = _notice_body(msg, reason)
            ok = sysmsg.send_system_text(broker, device.id, body, msg_id=mid)
            path = "system"
        else:
            continue
        logger.info(
            "receipt msg=%s bid=%s st=%s path=%s ok=%d", msg_id, bid, st, path, int(ok)
        )
        if ok:
            outcome = path
    return outcome


def send_moving_receipt(
    device: devices_store.Device, ref: str, broker: BrokerClient
) -> bool:
    """Refuses the up text `ref` with `rsn: "moving"` (docs/MOVE_DESIGN.md §12.5); not stored.

    False when the device has not reported `mb` (the caller sends the system reply instead) or
    the publish fails. The id derives from `ref`, so a re-sent text gets the same receipt."""
    if device.status.mb is None:
        return False
    obj: dict[str, Any] = {
        "v": 1,
        "id": sysmsg.derived_id("rcpt", device.id, ref, MOVING_REASON, "failed"),
        "ts": int(time.time()),
        "kind": "rcpt",
        "ref": ref,
        "st": "failed",
        "rsn": MOVING_REASON,
        "ack": None,
    }
    return broker.publish_down(device.id, obj)


def _notice_body(msg: messages_store.Message, reason: str) -> str:
    """`to <name> <h:mm>: not delivered (<reason>)`, <= 90 bytes; the name is cut first."""
    sender = users_store.get_user(msg.senderUid)
    when = clockfmt.clock(msg.ts, sender.familyId if sender is not None else None)
    why = sysmsg.clip_bytes(reason, NOTICE_REASON_MAX_BYTES)
    name = _recipient_name(msg)
    body = f"to {name} {when}: not delivered ({why})"
    while len(body.encode("utf-8")) > NOTICE_BODY_MAX_BYTES and len(name) > 1:
        name = name[:-1]
        body = f"to {name} {when}: not delivered ({why})"
    return body
