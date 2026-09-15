"""`users/{uid}/pushTokens/{token}`: FCM registration tokens, doc id = the token (idempotent)."""

from __future__ import annotations

from datetime import datetime

from google.cloud.firestore import SERVER_TIMESTAMP
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
    )


def remove_token(uid: str, token: str) -> None:
    _tokens(uid).document(token).delete()


def list_tokens(uid: str) -> list[str]:
    """Just the token strings, which the FCM call needs."""
    return [snap.id for snap in _tokens(uid).stream()]


def list_token_rows(uid: str) -> list[PushToken]:
    return [
        PushToken.model_validate({"token": snap.id, **(snap.to_dict() or {})})
        for snap in _tokens(uid).stream()
    ]
