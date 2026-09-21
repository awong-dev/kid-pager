"""`book` and `cfg` `/down` envelopes (docs/PROTOCOL.md §3.1/§3.2).
Pending state is kept in `devices/{d}.pending*` sub-fields; only the newest of each kind."""

from __future__ import annotations

import json
import logging
import time
from datetime import UTC, datetime
from typing import Any

from app import ca_resolve
from app.broker import BrokerClient
from app.config import Settings
from app.db.firestore import get_db
from app.ids import new_message_id
from app.store import backends as backends_store
from app.store import devices as devices_store
from app.store import users as users_store
from app.wirecbor import encode as cbor_encode
from app.wirecbor import to_json_safe

logger = logging.getLogger("relay.devcfg")

MAX_APPROVED_CONTACTS = 10

# docs/PROTOCOL.md §3.1: c[].n / p[].n are display names with a 16-code-point cap, truncated
# here rather than rejected since a stored `displayName` may already exceed it.
_BOOK_NAME_MAX_CODEPOINTS = 16

# docs/PROTOCOL.md §14.3: a CBOR `sig` pair is always exactly 10 bytes on the wire
# (`0x0D 0x48` + an 8-byte tag).
_SIGNED_CBOR_SIG_BYTES = 10
# Worst-case `n` (a 52-bit counter, `app/wire.py`'s `N_MAX_EXCLUSIVE`) for the size check,
# since the real value is only known at publish time.
_WORST_CASE_N = 2**53 - 1

_PENDING_BOOK_FIELD = "pendingBook"
_PENDING_CFG_FIELD = "pendingCfg"
# `cfg.ca` has its own pending slot so a pending lock and a pending CA push do not clobber
# each other. `lock` keeps the original `pendingCfg` name.
_PENDING_CFG_CA_FIELD = "pendingCfgCa"
# `cfg.sms` has its own pending slot, for the same reason.
_PENDING_CFG_SMS_FIELD = "pendingCfgSms"
# Every field `ack()`/`republish_pending()` iterate over: seven independent "newest unacked"
_ALL_PENDING_FIELDS = (
    _PENDING_BOOK_FIELD,
    _PENDING_CFG_FIELD,
    _PENDING_CFG_CA_FIELD,
    _PENDING_CFG_SMS_FIELD,
)


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




# `sig` is 11 base64url characters at runtime (8 raw bytes, `app/devauth.py`'s 64-bit
# truncated tag); `,"sig":"<11 chars>"` is the tail `sign_json` appends.
_SIGNED_JSON_SIG_SUFFIX_BYTES = len(',"sig":""') + 11


def _assert_within_envelope_limit(obj: dict[str, Any]) -> None:
    """Assert the signed envelope fits `MAX_ENVELOPE_BYTES` in both encodings (worst-case `n`)."""
    worst_case = {**obj, "n": _WORST_CASE_N}
    cbor_len = len(cbor_encode(worst_case)) + _SIGNED_CBOR_SIG_BYTES
    assert cbor_len <= MAX_ENVELOPE_BYTES, (
        f"devcfg {obj.get('kind')!r} payload too large: {cbor_len} bytes "
        f"signed CBOR (limit {MAX_ENVELOPE_BYTES})"
    )
    # `to_json_safe` mirrors `devauth.sign_json`: a raw `bytes` leaf (e.g. `cfg.ca.sha`) is
    # base64url text on the JSON wire.
    json_len = (
        len(
            json.dumps(
                to_json_safe(worst_case), separators=(",", ":"), ensure_ascii=False
            ).encode("utf-8")
        )
        + _SIGNED_JSON_SIG_SUFFIX_BYTES
    )
    assert json_len <= MAX_ENVELOPE_BYTES, (
        f"devcfg {obj.get('kind')!r} payload too large: {json_len} bytes "
        f"signed JSON (limit {MAX_ENVELOPE_BYTES})"
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


def push_ca(
    device_id: str, *, pem: str, broker: BrokerClient, settings: Settings | None = None
) -> bool:
    """`/down cfg.ca = {url, sha}` for `pem`; raises `PublicBaseUrlRequired` without a base URL."""
    if devices_store.get_device(device_id) is None:
        logger.warning("push_ca: no such device %s", device_id)
        return False
    settings = settings if settings is not None else Settings.from_env()
    url, sha = ca_resolve.ca_pointer(pem, settings)
    obj: dict[str, Any] = {
        "v": 1,
        "id": new_message_id(),
        "ts": int(time.time()),
        "kind": "cfg",
        "cfg": {"ca": {"url": url, "sha": sha}},
        "ack": None,
    }
    _assert_within_envelope_limit(obj)
    _set_pending(device_id, _PENDING_CFG_CA_FIELD, obj)
    return broker.publish_down(device_id, obj)


def unpin_ca(device_id: str, broker: BrokerClient) -> bool:
    """`url = ""` means un-pin (docs/PROTOCOL.md §3.2); no `sha`, since there is no CA."""
    if devices_store.get_device(device_id) is None:
        logger.warning("unpin_ca: no such device %s", device_id)
        return False
    obj: dict[str, Any] = {
        "v": 1,
        "id": new_message_id(),
        "ts": int(time.time()),
        "kind": "cfg",
        "cfg": {"ca": {"url": ""}},
        "ack": None,
    }
    _assert_within_envelope_limit(obj)
    _set_pending(device_id, _PENDING_CFG_CA_FIELD, obj)
    return broker.publish_down(device_id, obj)


def push_sms_contacts(
    device_id: str, contacts: list[dict[str, Any]], broker: BrokerClient
) -> bool:
    """`/down cfg.sms = [{n, p}, ...]`, the whole SMS allow-list every time (`[]` clears it)."""
    if devices_store.get_device(device_id) is None:
        logger.warning("push_sms_contacts: no such device %s", device_id)
        return False
    obj: dict[str, Any] = {
        "v": 1,
        "id": new_message_id(),
        "ts": int(time.time()),
        "kind": "cfg",
        "cfg": {"sms": [{"n": c["name"], "p": c["phone"]} for c in contacts]},
        "ack": None,
    }
    _assert_within_envelope_limit(obj)
    _set_pending(device_id, _PENDING_CFG_SMS_FIELD, obj)
    return broker.publish_down(device_id, obj)


def sms_pending(device_id: str) -> bool:
    """True iff this device has a pushed `cfg.sms` not yet acked `shown`."""
    snap = _devices().document(device_id).get()
    if not snap.exists:
        return False
    pending = (snap.to_dict() or {}).get(_PENDING_CFG_SMS_FIELD)
    return isinstance(pending, dict) and not pending.get("acked", False)


def ack(device_id: str, msg_id: str) -> bool:
    """True if `msg_id` is a pending book/cfg id (idempotent); False for an unknown id."""
    ref = _devices().document(device_id)
    snap = ref.get()
    if not snap.exists:
        return False
    data = snap.to_dict() or {}
    matched = False
    updates: dict[str, Any] = {}
    for field in _ALL_PENDING_FIELDS:
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
    for field in _ALL_PENDING_FIELDS:
        pending = data.get(field)
        if not isinstance(pending, dict) or pending.get("acked"):
            continue
        obj = pending.get("obj")
        if not isinstance(obj, dict):
            continue
        logger.info(
            "re-publishing unacked %s %s to device %s", obj.get("kind"), obj.get("id"), device_id
        )
