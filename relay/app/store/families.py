"""`families/{fid}`: the tenancy boundary; membership lives on `users`/`devices` via `familyId`."""

from __future__ import annotations

from datetime import datetime

from google.api_core.exceptions import NotFound
from google.cloud.firestore import SERVER_TIMESTAMP, ArrayUnion

from app.db.firestore import get_db


class Family(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    name: str
    blockedNumbers: list[str] = []
    createdAt: datetime | None = None
    createdBy: str | None = None


def _families():
    return get_db().collection("families")


def create_family(
    *,
    name: str,
    created_by: str,
    family_id: str | None = None,
) -> Family:
    """Creates `families/{fid}`; `family_id` pins the id, else Firestore assigns one."""
    ref = _families().document(family_id) if family_id else _families().document()
    ref.set(
        {
            "name": name,
            "blockedNumbers": [],
            "createdAt": SERVER_TIMESTAMP,
            "createdBy": created_by,
        }
    )
    fetched = get_family(ref.id)
    assert fetched is not None
    return fetched


def get_family(family_id: str) -> Family | None:
    snap = _families().document(family_id).get()
    if not snap.exists:
        return None
    return Family.model_validate({"id": family_id, **(snap.to_dict() or {})})


def list_families() -> list[Family]:
    return [
        Family.model_validate({"id": snap.id, **(snap.to_dict() or {})})
        for snap in _families().stream()
    ]


    updates: dict[str, object] = {}
    if name is not None:
        updates["name"] = name
    ref = _families().document(family_id)
    if updates:
        try:
            ref.update(updates)
        except NotFound as exc:
            raise KeyError(f"no such family: {family_id!r}") from exc
    fetched = get_family(family_id)
    if fetched is None:
        raise KeyError(f"no such family: {family_id!r}")
    return fetched


def add_blocked_number(family_id: str, phone: str) -> Family:
    """Adds `phone` to `blockedNumbers` (an idempotent `ArrayUnion`)."""
    ref = _families().document(family_id)
    try:
        ref.update({"blockedNumbers": ArrayUnion([phone])})
    except NotFound as exc:
        raise KeyError(f"no such family: {family_id!r}") from exc
    fetched = get_family(family_id)
    if fetched is None:
        raise KeyError(f"no such family: {family_id!r}")
    return fetched
