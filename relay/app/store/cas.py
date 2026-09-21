"""`cas/{sha256hex}`: CA PEMs the relay has handed out pointers for, keyed by the PEM's SHA-256.
Not secret, but no firestore.rules match block: the public read path is `GET /ca/{sha256hex}.pem`."""

from __future__ import annotations

from google.cloud.firestore import SERVER_TIMESTAMP

from app.db.firestore import get_db


def _cas():
    return get_db().collection("cas")


def remember(sha256hex: str, pem: str) -> None:
    """Idempotent upsert of `{pem, createdAt}`; the hash is the content, so `pem` never changes."""
    _cas().document(sha256hex).set({"pem": pem, "createdAt": SERVER_TIMESTAMP}, merge=True)


def get_pem(sha256hex: str) -> str | None:
    snap = _cas().document(sha256hex).get()
    if not snap.exists:
        return None
    pem = (snap.to_dict() or {}).get("pem")
    return pem if isinstance(pem, str) else None
