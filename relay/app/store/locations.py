"""`devices/{deviceId}/locations/{autoId}`: the collection primitive behind `app/location.py`."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.db.firestore import get_db


class LocationFix(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str | None = None
    ts: int
    fixTs: int
    lat: float
    lon: float
    accM: int | None = None
    src: str = "gnss"
    cached: bool = False
    reqId: str | None = None
    createdAt: datetime | None = None


def _locations(device_id: str):
    return get_db().collection("devices").document(device_id).collection("locations")


def locations_collection(device_id: str):
    """Public: `app/location.py` queries and pre-allocates refs inside its own transactions."""
    return _locations(device_id)


def new_location_ref(device_id: str):
    """A fresh doc ref in `devices/{device_id}/locations`, for staging inside a transaction."""
    return _locations(device_id).document()


def add_location(device_id: str, fix: LocationFix) -> str:
    from google.cloud.firestore import SERVER_TIMESTAMP

    doc = fix.model_dump(exclude={"id", "createdAt"})
    doc["createdAt"] = SERVER_TIMESTAMP
    _, ref = _locations(device_id).add(doc)
    return ref.id


def list_locations(device_id: str, *, limit: int = 50) -> list[LocationFix]:
    query = _locations(device_id).order_by("createdAt", direction="DESCENDING").limit(limit)
    return [
        LocationFix.model_validate({"id": snap.id, **(snap.to_dict() or {})})
        for snap in query.stream()
    ]


def latest_location(device_id: str) -> LocationFix | None:
    fixes = list_locations(device_id, limit=1)
    return fixes[0] if fixes else None
