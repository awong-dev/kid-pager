"""`devices/{deviceId}`. `locatableBy` is denormalised (recomputed by the allow store) for rules;
`status` is one embedded map overwritten on every /status."""

from __future__ import annotations

from datetime import datetime


from app.db.firestore import get_db


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
) -> Device:
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




    """Overwrites `status` with `fields` + `updatedAt`; `clear` names are deleted."""

    status = {k: v for k, v in fields.items() if v is not None}
    status["updatedAt"] = SERVER_TIMESTAMP
    _devices().document(device_id).set({"status": status}, merge=True)
