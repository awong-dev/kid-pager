"""`book` and `cfg` `/down` envelopes (docs/PROTOCOL.md §3.1/§3.2).
Pending state is kept in `devices/{d}.pending*` sub-fields; only the newest of each kind."""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from typing import Any

from app.broker import BrokerClient
from app.db.firestore import get_db
from app.ids import new_message_id
from app.store import backends as backends_store
from app.store import devices as devices_store
from app.store import users as users_store
from app.wirecbor import encode as cbor_encode

logger = logging.getLogger("relay.devcfg")

MAX_APPROVED_CONTACTS = 10

# docs/PROTOCOL.md §3.1: c[].n / p[].n are display names with a 16-code-point cap, truncated
# here rather than rejected since a stored `displayName` may already exceed it.
_BOOK_NAME_MAX_CODEPOINTS = 16

# docs/PROTOCOL.md §14.3: a CBOR `sig` pair is always exactly 10 bytes on the wire
# (`0x0D 0x48` + an 8-byte tag).
_SIGNED_CBOR_SIG_BYTES = 10

_PENDING_BOOK_FIELD = "pendingBook"
_PENDING_CFG_FIELD = "pendingCfg"


def _devices():
    return get_db().collection("devices")


def get_book_version(device_id: str) -> int:
    """Raw read of `devices/{d}.bookVersion` (not modelled on `devices_store.Device`)."""
    snap = _devices().document(device_id).get()
    if not snap.exists:
        return 0
    return int((snap.to_dict() or {}).get("bookVersion", 0) or 0)


def _default_alias(device: devices_store.Device) -> str | None:
    if device.defaultToUid is None:
        return None
    user = users_store.get_user(device.defaultToUid)
    return user.alias if user is not None else None


def _contact_type_hint(uid: str) -> str:
    kinds = {b.kind for b in backends_store.list_backends(uid) if b.enabled}
    if "gchat" in kinds:
        return "chat"
    return "web"




def _assert_within_envelope_limit(obj: dict[str, Any]) -> None:
    worst_case = {**obj, "n": _WORST_CASE_N}
        f"signed CBOR (limit {MAX_ENVELOPE_BYTES})"
    )


def build_book(device_id: str) -> dict[str, Any]:
    device = devices_store.get_device(device_id)
    if device is None:
        raise ValueError(f"no such device: {device_id!r}")

    obj: dict[str, Any] = {
        "v": 1,
        "id": new_message_id(),
        "ts": int(time.time()),
        "kind": "book",
        "bv": get_book_version(device_id),
    }
    default_alias = _default_alias(device)
    if default_alias is not None:
        obj["d"] = default_alias
    obj["ack"] = None
    _assert_within_envelope_limit(obj)
    return obj


def _set_pending(device_id: str, field: str, obj: dict[str, Any]) -> None:


        logger.warning("push_book: no such device %s", device_id)
        return False
    _set_pending(device_id, _PENDING_BOOK_FIELD, obj)
    return broker.publish_down(device_id, obj)


def push_cfg(device_id: str, lock: dict[str, Any], broker: BrokerClient) -> bool:
    """`lock` is the `{clear?: bool, auto?: int}` map `POST /api/admin/devices/{id}/cfg` accepts."""
    if devices_store.get_device(device_id) is None:
        logger.warning("push_cfg: no such device %s", device_id)
        return False
    obj: dict[str, Any] = {
        "v": 1,
        "id": new_message_id(),
        "ts": int(time.time()),
        "kind": "cfg",
        "cfg": {"lock": lock},
        "ack": None,
    }
    _assert_within_envelope_limit(obj)
    _set_pending(device_id, _PENDING_CFG_FIELD, obj)
    return broker.publish_down(device_id, obj)


def ack(device_id: str, msg_id: str) -> bool:
    """True if `msg_id` is a pending book/cfg id (idempotent); False for an unknown id."""
    ref = _devices().document(device_id)
    snap = ref.get()
    if not snap.exists:
        return False
    data = snap.to_dict() or {}
    matched = False
    updates: dict[str, Any] = {}
        pending = data.get(field)
        if isinstance(pending, dict) and pending.get("id") == msg_id:
            matched = True
            if not pending.get("acked"):
                updates[f"{field}.acked"] = True
    if updates:
        ref.update(updates)
    return matched


def republish_pending(device_id: str, broker: BrokerClient) -> None:
    """docs/PROTOCOL.md §5.3: re-publish the exact previously-built `obj` of each unacked slot."""
    snap = _devices().document(device_id).get()
    if not snap.exists:
        return
    data = snap.to_dict() or {}
        pending = data.get(field)
        if not isinstance(pending, dict) or pending.get("acked"):
            continue
        obj = pending.get("obj")
        if not isinstance(obj, dict):
            continue
        logger.info(
            "re-publishing unacked %s %s to device %s", obj.get("kind"), obj.get("id"), device_id
        )
