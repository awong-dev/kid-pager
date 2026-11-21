"""`settings/retention` and `settings/meta`; `meta.seqCounter` orders threads."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from google.api_core.exceptions import AlreadyExists
from google.cloud.firestore import SERVER_TIMESTAMP
from pydantic import BaseModel, ConfigDict, model_validator

from app.db.firestore import get_db

Unit = Literal["days", "weeks"]

# Retention is a plain day count per class, each with hard bounds; location has a ceiling the
# admin cannot lift.
BOUNDS: dict[str, tuple[int, int]] = {
    "messagesDays": (1, 365),
    "locationsDays": (1, 30),
    "alertsDays": (7, 365),
    "batteryDays": (7, 365),
}
DEFAULTS: dict[str, int] = {
    "messagesDays": 28,
    "locationsDays": 7,
    "alertsDays": 90,
    "batteryDays": 90,
}


class RetentionSetting(BaseModel):
    """The legacy `{n, unit}` shape; read from old docs, converted to days."""

    model_config = ConfigDict(extra="ignore")

    n: int
    unit: Unit

    def days(self) -> int:
        return self.n * (7 if self.unit == "weeks" else 1)


def _clamp(name: str, value: object) -> int:
    lo, hi = BOUNDS[name]
    if isinstance(value, bool) or not isinstance(value, int):
        return DEFAULTS[name]
    return max(lo, min(hi, value))


class RetentionSettings(BaseModel):
    """`settings/retention`. Reads clamp to `BOUNDS` and fold legacy `{n, unit}` into days."""

    model_config = ConfigDict(extra="ignore")

    messagesDays: int = DEFAULTS["messagesDays"]
    locationsDays: int = DEFAULTS["locationsDays"]
    alertsDays: int = DEFAULTS["alertsDays"]
    batteryDays: int = DEFAULTS["batteryDays"]

    @model_validator(mode="before")
    @classmethod
    def _normalise(cls, data: object) -> object:
        if not isinstance(data, dict):
            return data
        out = dict(data)
        for legacy, new in (("messages", "messagesDays"), ("locations", "locationsDays")):
            old = out.pop(legacy, None)
            if new not in out and isinstance(old, dict):
                try:
                    out[new] = RetentionSetting.model_validate(old).days()
                except ValueError:
                    pass
        for name in BOUNDS:
            if name in out:
                out[name] = _clamp(name, out[name])
        return out


class LastSweep(BaseModel):
    """`settings/meta.lastSweep`."""

    model_config = ConfigDict(extra="ignore")

    at: datetime | None = None
    counts: dict[str, int] = {}
    # oldest surviving `createdAt` per class, None when the class is empty
    oldest: dict[str, datetime | None] = {}


class MetaSettings(BaseModel):
    model_config = ConfigDict(extra="ignore")

    schemaVersion: int = 2
    lastSweepAt: datetime | None = None
    # The in-process scheduler's last tick; never written in the cloud.
    lastTickAt: datetime | None = None
    # The last hourly location pass.
    lastLocPassAt: datetime | None = None
    lastSweep: LastSweep | None = None
    seqCounter: int = 0


def _settings():
    return get_db().collection("settings")


def get_retention() -> RetentionSettings:
    snap = _settings().document("retention").get()
    if not snap.exists:
        return RetentionSettings()
    return RetentionSettings.model_validate(snap.to_dict() or {})


def set_retention(
    *,
    messagesDays: int | None = None,
    locationsDays: int | None = None,
    alertsDays: int | None = None,
    batteryDays: int | None = None,
    messages: RetentionSetting | None = None,
    locations: RetentionSetting | None = None,
) -> RetentionSettings:
    """Merge write of the fields passed; `ValueError` outside `BOUNDS`; legacy fields dropped."""
    if messages is not None and messagesDays is None:
        messagesDays = messages.days()
    if locations is not None and locationsDays is None:
        locationsDays = locations.days()
    current = get_retention().model_dump()
    for name, value in (
        ("messagesDays", messagesDays),
        ("locationsDays", locationsDays),
        ("alertsDays", alertsDays),
        ("batteryDays", batteryDays),
    ):
        if value is None:
            continue
        lo, hi = BOUNDS[name]
        if not lo <= value <= hi:
            raise ValueError(f"{name} must be {lo}..{hi}")
        current[name] = value
    _settings().document("retention").set(current)
    return get_retention()


def get_meta() -> MetaSettings:
    snap = _settings().document("meta").get()
    if not snap.exists:
        return MetaSettings()
    return MetaSettings.model_validate(snap.to_dict() or {})


def mark_swept(report: dict | None = None) -> None:
    """Sets `lastSweepAt = now` and `lastSweep = report` (merge: safe if `meta` is absent)."""
    data: dict = {"lastSweepAt": SERVER_TIMESTAMP}
    if report is not None:
        data["lastSweep"] = report
    _settings().document("meta").set(data, merge=True)


def mark_loc_pass() -> None:
    """`settings/meta.lastLocPassAt = now` -- the hourly location-class pass."""
    _settings().document("meta").set({"lastLocPassAt": SERVER_TIMESTAMP}, merge=True)


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
