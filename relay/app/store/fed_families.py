"""`fedFamilies/{fid}`: a family's federation identity (key pair, `fedId`, `epoch`), server-only.
`fedId` is stable; a move rotates the key and keeps the old one as `prevPrivateKey`."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from google.api_core.exceptions import AlreadyExists
from google.cloud.firestore import SERVER_TIMESTAMP, FieldFilter
from pydantic import BaseModel, ConfigDict

from app.db.firestore import get_db
from app.fed import keys


class FedFamily(BaseModel):
    model_config = ConfigDict(extra="ignore")

    familyId: str
    fedId: str
    publicKey: str
    privateKey: str
    epoch: int = 0
    prevPrivateKey: str | None = None
    createdAt: datetime | None = None

    def fingerprint(self) -> str:
        return keys.fingerprint(self.publicKey)


def _coll():
    return get_db().collection("fedFamilies")


def _model(snap) -> FedFamily:
    return FedFamily.model_validate({"familyId": snap.id, **(snap.to_dict() or {})})


def get(family_id: str) -> FedFamily | None:
    snap = _coll().document(family_id).get()
    return _model(snap) if snap.exists else None


def get_by_fed_id(fed_id: str) -> FedFamily | None:
    for snap in _coll().where(filter=FieldFilter("fedId", "==", fed_id)).stream():
        return _model(snap)
    return None


def get_or_create(family_id: str) -> FedFamily:
    found = get(family_id)
    if found is not None:
        return found
    private, public = keys.generate()
    try:
        _coll().document(family_id).create(
            {
                "familyId": family_id,
                "fedId": keys.new_fed_id(),
                "publicKey": public,
                "privateKey": private,
                "epoch": 0,
                "prevPrivateKey": None,
                "createdAt": SERVER_TIMESTAMP,
            }
        )
    except AlreadyExists:
        pass
    found = get(family_id)
    assert found is not None
    return found


def clear_prev(family_id: str) -> None:
    """Every link has accepted the new key: the old private key is no longer needed."""
    ref = _coll().document(family_id)
    if ref.get().exists:
        ref.update({"prevPrivateKey": None})


def rotated(data: dict[str, Any]) -> dict[str, Any]:
    """Row `data` after a move: new key pair, `epoch + 1`, old key as `prevPrivateKey`. Pure."""
    private, public = keys.generate()
    return {
        **data,
        "publicKey": public,
        "privateKey": private,
        "epoch": int(data.get("epoch") or 0) + 1,
        "prevPrivateKey": data.get("privateKey"),
    }


def delete(family_id: str) -> None:
    _coll().document(family_id).delete()
