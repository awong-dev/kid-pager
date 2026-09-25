"""`deviceSecrets/{deviceId}`: HMAC key, MQTT password hash and replay counters, server-only.
`upN` is the highest accepted `n`, `upBits` a bitmap of the 64 below it (docs/PROTOCOL.md §14.2)."""

from __future__ import annotations

import base64
from datetime import datetime

from google.cloud.firestore import SERVER_TIMESTAMP, Transaction

from app.db.firestore import get_db, run_transaction

_WINDOW = 64
_WINDOW_MASK = (1 << _WINDOW) - 1

def _to_signed64(bits: int) -> int:
    """Firestore ints are signed int64: a bitmap with bit 63 set is stored as two's complement."""
    bits &= _WINDOW_MASK
    return bits - (1 << 64) if bits >= (1 << 63) else bits


def _from_signed64(stored: int) -> int:
    return (stored + (1 << 64)) & _WINDOW_MASK if stored < 0 else stored & _WINDOW_MASK


class DeviceSecret(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    hmacKey: bytes
    mqttPasswordHash: str
    upN: int = 0
    upBits: int = 0
    downN: int = 0
    sigFailures: int = 0
    createdAt: datetime | None = None
    rotatedAt: datetime | None = None


def _secrets():
    return get_db().collection("deviceSecrets")


def _decode(device_id: str, data: dict) -> DeviceSecret:
    fields = dict(data)
    hmac_b64 = fields.pop("hmacKey", "")
    fields["hmacKey"] = base64.b64decode(hmac_b64) if hmac_b64 else b""
    if "upBits" in fields:
        fields["upBits"] = _from_signed64(fields["upBits"])
    return DeviceSecret.model_validate({"id": device_id, **fields})


def create(device_id: str, *, hmac_key: bytes, mqtt_password_hash: str) -> DeviceSecret:
    ref = _secrets().document(device_id)
    ref.set(
        {
            "hmacKey": base64.b64encode(hmac_key).decode("ascii"),
            "mqttPasswordHash": mqtt_password_hash,
            "upN": 0,
            "upBits": 0,
            "downN": 0,
            "sigFailures": 0,
            "createdAt": SERVER_TIMESTAMP,
        }
    )
    fetched = get(device_id)
    assert fetched is not None
    return fetched


def get(device_id: str) -> DeviceSecret | None:
    snap = _secrets().document(device_id).get()
    if not snap.exists:
        return None
    return _decode(device_id, snap.to_dict() or {})


def rotate(device_id: str, hmac_key: bytes, mqtt_password_hash: str) -> DeviceSecret:
    """Replaces both secrets and zeroes every counter."""
    ref = _secrets().document(device_id)
    if not ref.get().exists:
        raise KeyError(f"no such device secret: {device_id!r}")
    ref.update(
        {
            "hmacKey": base64.b64encode(hmac_key).decode("ascii"),
            "mqttPasswordHash": mqtt_password_hash,
            "upN": 0,
            "upBits": 0,
            "downN": 0,
            "sigFailures": 0,
            "rotatedAt": SERVER_TIMESTAMP,
        }
    )
    fetched = get(device_id)
    assert fetched is not None
    return fetched


def delete(device_id: str) -> None:
    _secrets().document(device_id).delete()


def accept_up_n(device_id: str, n: int) -> bool:
    """Accepts `n > upN` or a clear gap bit with `0 < upN - n <= 64`; False on a replay."""
    ref = _secrets().document(device_id)

    def _txn(transaction: Transaction) -> bool:
        snap = ref.get(transaction=transaction)
        if not snap.exists:
            raise KeyError(f"no such device secret: {device_id!r}")
        data = snap.to_dict() or {}
        up_n = data.get("upN", 0)
        up_bits = _from_signed64(data.get("upBits", 0))

        if n > up_n:
            shift = n - up_n
            new_bits = ((up_bits << shift) | (1 << (shift - 1))) & _WINDOW_MASK
            transaction.update(ref, {"upN": n, "upBits": _to_signed64(new_bits)})
            return True

        gap = up_n - n
        if 0 < gap <= _WINDOW:
            bit = 1 << (gap - 1)
            if up_bits & bit:
                return False
            transaction.update(ref, {"upBits": _to_signed64(up_bits | bit)})
            return True

        return False

    return run_transaction(_txn)


def accept_request_n(device_id: str, n: int) -> bool:
    """Book-pull check (docs/PROTOCOL.md §14.7): only `n > upN`, no gap window; else no write."""
    ref = _secrets().document(device_id)

    def _txn(transaction: Transaction) -> bool:
        snap = ref.get(transaction=transaction)
        if not snap.exists:
            raise KeyError(f"no such device secret: {device_id!r}")
        data = snap.to_dict() or {}
        up_n = data.get("upN", 0)
        up_bits = _from_signed64(data.get("upBits", 0))

        if n > up_n:
            shift = n - up_n
            new_bits = ((up_bits << shift) | (1 << (shift - 1))) & _WINDOW_MASK
            transaction.update(ref, {"upN": n, "upBits": _to_signed64(new_bits)})
            return True

        return False

    return run_transaction(_txn)


def next_down_n(device_id: str) -> int:
    """Issues the next `n` for a `/down` envelope."""
    ref = _secrets().document(device_id)

    def _txn(transaction: Transaction) -> int:
        snap = ref.get(transaction=transaction)
        if not snap.exists:
            raise KeyError(f"no such device secret: {device_id!r}")
        data = snap.to_dict() or {}
        down_n = data.get("downN", 0) + 1
        transaction.update(ref, {"downN": down_n})
        return down_n

    return run_transaction(_txn)


def bump_sig_failures(device_id: str) -> int:
    """Increments and returns the signature-failure counter."""
    ref = _secrets().document(device_id)

    def _txn(transaction: Transaction) -> int:
        snap = ref.get(transaction=transaction)
        if not snap.exists:
            raise KeyError(f"no such device secret: {device_id!r}")
        data = snap.to_dict() or {}
        count = data.get("sigFailures", 0) + 1
        transaction.update(ref, {"sigFailures": count})
        return count

    return run_transaction(_txn)
