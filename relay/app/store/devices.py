"""`devices/{deviceId}`. `locatableBy` is denormalised (recomputed by the allow store) for rules;
`status` is one embedded map overwritten on every /status."""

from __future__ import annotations

import re
import time
from datetime import datetime
from typing import Literal

from google.cloud.firestore import FieldFilter, Transaction
from pydantic import BaseModel, ConfigDict, field_validator

from app.db.firestore import get_db
from app.wire import CellInfo

# `phone` is a real E.164 number, never an alias reference (same shape as `app/wire.py`'s
_SMS_PHONE_RE = re.compile(r"^\+[1-9]\d{6,14}$")
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x1f\x7f]")
SMS_CONTACT_NAME_MAX_CODEPOINTS = 16
# Names are capped at 24 UTF-8 bytes so 8 maximal `cfg.sms` entries fit the 640-byte signed
# envelope limit (570 bytes JSON, 421 CBOR; docs/PROTOCOL.md §3.3).
SMS_CONTACT_NAME_MAX_UTF8_BYTES = 24
MAX_SMS_CONTACTS = 8

# More than 20 failures in 10 minutes on one device raises `status.authAlarm`.
AUTH_ALARM_THRESHOLD = 20
AUTH_ALARM_WINDOW_S = 600


class LastCell(BaseModel):
    """The pager's last reported serving cell, resolved or not (docs/PROTOCOL.md §13.2)."""

    model_config = ConfigDict(extra="ignore")

    mcc: str
    mnc: str
    tac: int
    ci: int
    rsrp: int | None = None
    ts: int | None = None


class DeviceStatus(BaseModel):
    model_config = ConfigDict(extra="ignore")

    state: str | None = None
    mode: str | None = None
    battMv: int | None = None
    rssi: int | None = None
    session: str | None = None
    ts: int | None = None
    fw: str | None = None
    locPeriodS: int | None = None
    locMinS: int | None = None
    # Trust state and the pinned CA's short fingerprint.
    caFp: str | None = None
    # The device's own reported location backoff.
    locBackoffS: int | None = None
    # GNSS-while-moving interval (s) reported while tracking is on, 0/absent = off; display only
    # (docs/PROTOCOL.md §13.3).
    locMoveS: int | None = None
    # Device SMS audit-queue drop counter.
    smsLost: int | None = None
    # MQTT-session generation within the current boot; a changed `link` triggers the online-edge
    # republish like a changed `session`.
    link: int | None = None
    # Crash diagnostics (docs/PROTOCOL.md §5.1): display only, stored as reported.
    rst: int | None = None
    stage: int | None = None
    abn: int | None = None
    # Crash diagnostics (docs/PROTOCOL.md §5.1): the AT command the main loop was stuck on before
    # the last abnormal reset.
    stallcmd: str | None = None
    # `bpull` (docs/PROTOCOL.md §3.7/§5.1): nudges are sent only while the last online `/status`
    # carried it; only ever `1`.
    bpull: int | None = None
    updatedAt: datetime | None = None
    # Set once `sigFailures` crosses AUTH_ALARM_THRESHOLD within AUTH_ALARM_WINDOW_S; cleared on
    # key rotation.
    authAlarm: bool | None = None
    lastCell: LastCell | None = None


class SmsContact(BaseModel):
    """One `smsContacts` entry: `name` 1-16 code points, no control characters; `phone` E.164."""

    model_config = ConfigDict(extra="ignore")

    name: str
    phone: str

    @field_validator("name")
    @classmethod
    def _check_name(cls, value: str) -> str:
        if not (1 <= len(value) <= SMS_CONTACT_NAME_MAX_CODEPOINTS):
            raise ValueError(
                f"sms contact name {value!r} must be 1-{SMS_CONTACT_NAME_MAX_CODEPOINTS} "
                "characters"
            )
        if _CONTROL_CHAR_RE.search(value):
            raise ValueError(f"sms contact name {value!r} contains control characters")
        if len(value.encode("utf-8")) > SMS_CONTACT_NAME_MAX_UTF8_BYTES:
            raise ValueError(
                f"sms contact name {value!r} exceeds {SMS_CONTACT_NAME_MAX_UTF8_BYTES} UTF-8 bytes"
            )
        return value

    @field_validator("phone")
    @classmethod
    def _check_phone(cls, value: str) -> str:
        if not _SMS_PHONE_RE.match(value):
            raise ValueError(f"sms contact phone {value!r} is not a valid E.164 number")
        return value


class PendingPush(BaseModel):
    """A "newest unacked" push slot: `id`, the exact unsigned envelope `obj`, `acked`."""

    model_config = ConfigDict(extra="ignore")

    id: str
    obj: dict[str, object]
    acked: bool


class Device(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    ownerUid: str
    label: str
    mqttUsername: str
    defaultToUid: str | None = None
    revokedAt: datetime | None = None
    locatableBy: list[str] = []
    status: DeviceStatus = DeviceStatus()
    # The SMS contact allow-list, the relay's source of truth, pushed as `/down cfg.sms` on every
    # change; `pendingCfgSms` tracks what the device has acked.
    smsContacts: list[SmsContact] = []
    # Carrier APN baked into this device's setup codes and bundles
    # (app/apn_presets.py). None = carrier default.
    apn: str | None = None
    # "password" is the unsigned legacy device; "hmac" signs every `/up`, `/status` and `/loc`
    # envelope with its `deviceSecrets` key.
    authMode: Literal["password", "hmac"] = "hmac"
    # Encoding of the device's last accepted envelope, which `/down` answers in; `None` until then.
    wire: Literal["json", "cbor"] | None = None
    # "issued" once a setup code exists for this device (create and rotate); "provisioned" on
    # its first signed `/status`. `None` on devices created before the field existed.
    provisionState: Literal["issued", "provisioned"] | None = None
    # Copied from the owner's `familyId` at creation; `None` on devices that predate it.
    familyId: str | None = None
    # Written directly via `get_db()` by the contacts store; modelled so `get_device()` returns it.
    bookVersion: int = 0
    # The "newest unacked" pending-push slots (book/lock/ca/sms/loc/ota/ast), written directly
    pendingBook: PendingPush | None = None
    pendingCfg: PendingPush | None = None
    pendingCfgCa: PendingPush | None = None
    pendingCfgSms: PendingPush | None = None


def _devices():
    return get_db().collection("devices")


def create_device(
    *,
    device_id: str,
    owner_uid: str,
    label: str,
    mqtt_username: str,
    mqtt_password_hash: str,
    default_to_uid: str | None = None,
    auth_mode: Literal["password", "hmac"] = "hmac",
    apn: str | None = None,
    family_id: str | None = None,
) -> Device:
    """`mqtt_password_hash` is accepted but not stored; the hash lives in `deviceSecrets/{d}`."""
    ref = _devices().document(device_id)
    ref.set(
        {
            "ownerUid": owner_uid,
            "label": label,
            "apn": apn,
            "mqttUsername": mqtt_username,
            "defaultToUid": default_to_uid,
            "revokedAt": None,
            "locatableBy": [],
            "status": {},
            "authMode": auth_mode,
            "wire": None,
            "provisionState": "issued",
            "familyId": family_id,
        }
    )
    fetched = get_device(device_id)
    assert fetched is not None
    return fetched


def get_device(device_id: str) -> Device | None:
    snap = _devices().document(device_id).get()
    if not snap.exists:
        return None
    return Device.model_validate({"id": device_id, **(snap.to_dict() or {})})


def list_devices(owner_uid: str | None = None) -> list[Device]:
    query = _devices()
    if owner_uid is not None:
        query = query.where(filter=FieldFilter("ownerUid", "==", owner_uid))
    return [
        Device.model_validate({"id": snap.id, **(snap.to_dict() or {})}) for snap in query.stream()
    ]


def delete_device(device_id: str) -> None:
    _devices().document(device_id).delete()


def revoke_device(device_id: str) -> Device:
    from google.cloud.firestore import SERVER_TIMESTAMP

    _devices().document(device_id).update({"revokedAt": SERVER_TIMESTAMP})
    fetched = get_device(device_id)
    if fetched is None:
        raise KeyError(f"no such device: {device_id!r}")
    return fetched


def set_locatable_by(device_id: str, uids: list[str]) -> None:
    _devices().document(device_id).update({"locatableBy": uids})


def set_sms_contacts(device_id: str, contacts: list[SmsContact]) -> None:
    """Stores the whole SMS contact list, replacing the old one; the API has validated it."""
    _devices().document(device_id).set(
        {"smsContacts": [c.model_dump() for c in contacts]}, merge=True
    )


def set_apn(device_id: str, apn: str | None) -> None:
    """The caller has validated `apn`; it takes effect at the next setup code, nothing is pushed."""
    _devices().document(device_id).set({"apn": apn}, merge=True)


def set_provision_state(device_id: str, state: Literal["issued", "provisioned"]) -> None:
    """Written "issued" on create and rotate; "provisioned" on the first signed `/status`."""
    _devices().document(device_id).set({"provisionState": state}, merge=True)


def set_wire(device_id: str, wire: Literal["json", "cbor"]) -> None:
    """Set from each accepted inbound envelope's encoding so `/down` answers in kind."""
    _devices().document(device_id).set({"wire": wire}, merge=True)


def record_sig_failure(device_id: str, now: float | None = None) -> int:
    """Returns the failures in the trailing `AUTH_ALARM_WINDOW_S`, this one included."""
    now = now if now is not None else time.time()
    ref = _devices().document(device_id)
    snap = ref.get()
    data = (snap.to_dict() or {}) if snap.exists else {}
    cutoff = now - AUTH_ALARM_WINDOW_S
    times = [
        t for t in data.get("authFailureTimes", []) if isinstance(t, (int, float)) and t > cutoff
    ]
    times.append(now)
    # Capped so a sustained attack cannot grow the list; only the last threshold's worth matters.
    times = times[-AUTH_ALARM_THRESHOLD:]
    ref.set({"authFailureTimes": times}, merge=True)
    return len(times)


def set_auth_alarm(device_id: str, value: bool) -> None:
    _devices().document(device_id).set({"status": {"authAlarm": value}}, merge=True)


def clear_auth_alarm(device_id: str) -> None:
    """Clears the auth alarm; called on key rotation."""
    _devices().document(device_id).set(
        {"authFailureTimes": [], "status": {"authAlarm": False}}, merge=True
    )


def read_last_cell_ts(device_id: str, transaction: Transaction) -> int | None:
    """Read half of `set_last_cell`'s monotonic check (a transaction reads before it writes)."""
    snap = _devices().document(device_id).get(transaction=transaction)
    if not snap.exists:
        return None
    last_cell = ((snap.to_dict() or {}).get("status") or {}).get("lastCell") or {}
    return last_cell.get("ts")


def set_last_cell(
    device_id: str, cell: CellInfo, ts: int, transaction: Transaction, *, existing_ts: int | None
) -> None:
    """Stages the last-cell write only if `existing_ts` is missing or <= `ts` (QoS 0 reorders)."""
    if existing_ts is not None and existing_ts > ts:
        return
    transaction.set(
        _devices().document(device_id),
        {
            "status": {
                "lastCell": {
                    "mcc": cell.mcc,
                    "mnc": cell.mnc,
                    "tac": cell.tac,
                    "ci": cell.ci,
                    "rsrp": cell.rsrp,
                    "ts": ts,
                }
            }
        },
        merge=True,
    )


    """Overwrites `status` with `fields` + `updatedAt`; `clear` names are deleted."""

    status = {k: v for k, v in fields.items() if v is not None}
    status["updatedAt"] = SERVER_TIMESTAMP
    _devices().document(device_id).set({"status": status}, merge=True)
