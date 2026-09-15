"""`devices/{deviceId}`. `locatableBy` is denormalised (recomputed by the allow store) for rules;
`status` is one embedded map overwritten on every /status."""

from __future__ import annotations

import time
from datetime import datetime
from typing import Literal


from app.db.firestore import get_db

# More than 20 failures in 10 minutes on one device raises `status.authAlarm`.
AUTH_ALARM_THRESHOLD = 20
AUTH_ALARM_WINDOW_S = 600


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
    updatedAt: datetime | None = None
    # Set once `sigFailures` crosses AUTH_ALARM_THRESHOLD within AUTH_ALARM_WINDOW_S; cleared on
    # key rotation.
    authAlarm: bool | None = None


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
    # "password" is the unsigned legacy device; "hmac" signs every `/up`, `/status` and `/loc`
    # envelope with its `deviceSecrets` key.
    authMode: Literal["password", "hmac"] = "hmac"
    # Encoding of the device's last accepted envelope, which `/down` answers in; `None` until then.
    wire: Literal["json", "cbor"] | None = None
    # "issued" once a setup code exists for this device (create and rotate); "provisioned" on
    # its first signed `/status`. `None` on devices created before the field existed.
    provisionState: Literal["issued", "provisioned"] | None = None


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
) -> Device:
    """`mqtt_password_hash` is accepted but not stored; the hash lives in `deviceSecrets/{d}`."""
    ref = _devices().document(device_id)
    ref.set(
        {
            "ownerUid": owner_uid,
            "label": label,
            "mqttUsername": mqtt_username,
            "defaultToUid": default_to_uid,
            "revokedAt": None,
            "locatableBy": [],
            "status": {},
            "authMode": auth_mode,
            "wire": None,
            "provisionState": "issued",
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


    """Overwrites `status` with `fields` + `updatedAt`; `clear` names are deleted."""

    status = {k: v for k, v in fields.items() if v is not None}
    status["updatedAt"] = SERVER_TIMESTAMP
    _devices().document(device_id).set({"status": status}, merge=True)
