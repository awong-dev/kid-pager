"""`settings/retention` and `settings/meta`; `meta.seqCounter` orders threads."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from google.api_core.exceptions import AlreadyExists
from google.cloud.firestore import SERVER_TIMESTAMP

from app.db.firestore import get_db

Unit = Literal["days", "weeks"]



class RetentionSetting(BaseModel):
    model_config = ConfigDict(extra="ignore")

    n: int
    unit: Unit


class RetentionSettings(BaseModel):
    model_config = ConfigDict(extra="ignore")



class MetaSettings(BaseModel):
    model_config = ConfigDict(extra="ignore")

    schemaVersion: int = 2
    lastSweepAt: datetime | None = None
    # The in-process scheduler's last tick; never written in the cloud.
    lastTickAt: datetime | None = None
    seqCounter: int = 0


def _settings():
    return get_db().collection("settings")


def get_retention() -> RetentionSettings:
    snap = _settings().document("retention").get()
    if not snap.exists:
        return RetentionSettings()
    return RetentionSettings.model_validate(snap.to_dict() or {})




def get_meta() -> MetaSettings:
    snap = _settings().document("meta").get()
    if not snap.exists:
        return MetaSettings()
    return MetaSettings.model_validate(snap.to_dict() or {})




def mark_ticked() -> None:
    """`settings/meta.lastTickAt = now`, after each in-process scheduler tick."""
    _settings().document("meta").set({"lastTickAt": SERVER_TIMESTAMP}, merge=True)


def ensure_meta_initialized() -> MetaSettings:
    """Idempotently creates `settings/meta` with defaults if absent."""
    ref = _settings().document("meta")
    try:
        # `create()`, not exists-check + `set()`: a racing `set()` could reset `seqCounter` to 0
        # and duplicate a `seq`.
        ref.create(MetaSettings().model_dump())
    except AlreadyExists:
        pass
    return get_meta()
