"""`cells/{mcc}-{mnc}-{tac}-{ci}`: cache of resolved cell positions (docs/PROTOCOL.md §13.2).
Server-only; a position is reused 30 days, an unknown cell 1 day."""

from __future__ import annotations

from datetime import UTC, datetime

from google.cloud.firestore import SERVER_TIMESTAMP
from pydantic import BaseModel, ConfigDict

from app.db.firestore import get_db

# docs/PROTOCOL.md §13.2: reuse resolved positions for 30 days, unknown cells for 1 day.
POSITIVE_TTL_S = 30 * 24 * 3600
NEGATIVE_TTL_S = 24 * 3600


class CellCacheEntry(BaseModel):
    """`unknown=True` is the negative cache: the provider was asked and does not know the cell."""

    model_config = ConfigDict(extra="ignore")

    lat: float | None = None
    lon: float | None = None
    accM: int | None = None
    provider: str | None = None
    unknown: bool = False
    resolvedAt: datetime | None = None


def cache_key(mcc: str, mnc: str, tac: int, ci: int) -> str:
    return f"{mcc}-{mnc}-{tac}-{ci}"


def _cells():
    return get_db().collection("cells")


def get_cached(mcc: str, mnc: str, tac: int, ci: int) -> CellCacheEntry | None:
    """None means no usable entry (never stored or past its TTL); an `unknown=True` hit counts."""
    snap = _cells().document(cache_key(mcc, mnc, tac, ci)).get()
    if not snap.exists:
        return None
    entry = CellCacheEntry.model_validate(snap.to_dict() or {})
    if entry.resolvedAt is None:
        return None
    ttl = NEGATIVE_TTL_S if entry.unknown else POSITIVE_TTL_S
    age = (datetime.now(UTC) - entry.resolvedAt).total_seconds()
    if age >= ttl:
        return None
    return entry


def remember_resolved(
    mcc: str, mnc: str, tac: int, ci: int, *, lat: float, lon: float, acc_m: int, provider: str
) -> None:
    _cells().document(cache_key(mcc, mnc, tac, ci)).set(
        {
            "lat": lat,
            "lon": lon,
            "accM": acc_m,
            "provider": provider,
            "unknown": False,
            "resolvedAt": SERVER_TIMESTAMP,
        }
    )


def remember_unknown(mcc: str, mnc: str, tac: int, ci: int, *, provider: str) -> None:
    _cells().document(cache_key(mcc, mnc, tac, ci)).set(
        {
            "lat": None,
            "lon": None,
            "accM": None,
            "provider": provider,
            "unknown": True,
            "resolvedAt": SERVER_TIMESTAMP,
        }
    )
