"""`users/{uid}/pushTokens/{token}`: FCM registration tokens, doc id = the token (idempotent)."""

from __future__ import annotations

from datetime import datetime

from google.cloud.firestore import SERVER_TIMESTAMP
from google.cloud.firestore_v1.base_query import FieldFilter
from pydantic import BaseModel, ConfigDict

from app.db.firestore import get_db


class PushToken(BaseModel):
    model_config = ConfigDict(extra="ignore")

    token: str
    createdAt: datetime | None = None
    errorCount: int = 0


def _tokens(uid: str):
    return get_db().collection("users").document(uid).collection("pushTokens")


def add_token(uid: str, token: str) -> None:
    """Idempotent: resets `errorCount`, keeps `createdAt`; `token` is mirrored for group queries."""
    _tokens(uid).document(token).set(
        {"token": token, "createdAt": SERVER_TIMESTAMP, "errorCount": 0}, merge=True
    )


def remove_token(uid: str, token: str) -> None:
    _tokens(uid).document(token).delete()


def remove_token_anywhere(token: str) -> bool:
    """Deletes a token doc without knowing its owner (group query on `token`); True if found."""
    db = get_db()
    matches = list(
        db.collection_group("pushTokens").where(filter=FieldFilter("token", "==", token)).stream()
    )
    for snap in matches:
        snap.reference.delete()
    return bool(matches)


def list_tokens(uid: str) -> list[str]:
    """Just the token strings, which the FCM call needs."""
    return [snap.id for snap in _tokens(uid).stream()]


def list_token_rows(uid: str) -> list[PushToken]:
    return [
        PushToken.model_validate({"token": snap.id, **(snap.to_dict() or {})})
        for snap in _tokens(uid).stream()
    ]
