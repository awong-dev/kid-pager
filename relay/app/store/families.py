"""`families/{fid}`: the tenancy boundary; membership lives on `users`/`devices` via `familyId`."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from google.api_core.exceptions import NotFound
from google.cloud.firestore import SERVER_TIMESTAMP, ArrayUnion
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app import wire
from app.db.firestore import get_db


class NoticeSettings(BaseModel):
    model_config = ConfigDict(extra="ignore")

    # The pager copy of a system notice; the web alert always happens.
    pager: bool = True


class TopupSettings(BaseModel):
    model_config = ConfigDict(extra="ignore")

    # `off` (default) | `limited` (`perMonth` per bridge per calendar month, 1..10) | `unlimited`.
    # `perMonth` only matters when `limited`.
    mode: Literal["off", "limited", "unlimited"] = "off"
    perMonth: int = Field(default=1, ge=1, le=10)


class FamilySettings(BaseModel):
    model_config = ConfigDict(extra="ignore")

    notices: NoticeSettings = NoticeSettings()
    # Active link probes (they light up the phone screen), default on.
    linkChecks: bool = True
    topup: TopupSettings = TopupSettings()


class NoticeSettingsPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pager: bool | None = None


class TopupSettingsPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["off", "limited", "unlimited"] | None = None
    perMonth: int | None = Field(default=None, ge=1, le=10)


class FamilySettingsPatch(BaseModel):
    """A partial edit: only the fields sent are written (dotted-path update)."""

    model_config = ConfigDict(extra="forbid")

    notices: NoticeSettingsPatch | None = None
    linkChecks: bool | None = None
    topup: TopupSettingsPatch | None = None

    @model_validator(mode="after")
    def _no_nulls(self) -> FamilySettingsPatch:
        # `exclude_unset` keeps an explicit null; none of these fields may be null.
        for name in self.model_fields_set:
            if getattr(self, name) is None:
                raise ValueError(f"settings.{name} must not be null")
        for sub_name in ("notices", "topup"):
            sub = getattr(self, sub_name)
            if sub is not None:
                for field in sub.model_fields_set:
                    if getattr(sub, field) is None:
                        raise ValueError(f"settings.{sub_name}.{field} must not be null")
        return self

    def dotted(self) -> dict[str, object]:
        """`{"settings.topup.mode": "limited", ...}` for `DocumentReference.update`."""
        out: dict[str, object] = {}
        for name, value in self.model_dump(exclude_unset=True).items():
            if isinstance(value, dict):
                for field, leaf in value.items():
                    out[f"settings.{name}.{field}"] = leaf
            else:
                out[f"settings.{name}"] = value
        return out


class Family(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    name: str
    blockedNumbers: list[str] = []
    settings: FamilySettings = FamilySettings()
    createdAt: datetime | None = None
    createdBy: str | None = None
    # Set while a family is being moved to another server; ingest, `Routing.send` and bridge
    # events refuse.
    frozen: bool = False
    # The family id that ids were derived from (set once on an import under `--as`); derivations
    # use `idSeed or id`.
    idSeed: str | None = None
    # Written first by an import, cleared last.
    importing: bool = False
    # The stored per-family federation switch (set by a super); the effective state is
    # `app.store.fed_state.family_enabled`.
    federation: bool = False

    @property
    def seed(self) -> str:
        return self.idSeed or self.id


class FamilyFrozen(Exception):
    """The family is frozen for a move."""


FROZEN_REPLY = "moving servers, try later"


FAMILY_NAME_MAX_CODEPOINTS = 40


def validate_family_name(raw: str) -> str:
    """Strip; 1-40 code points; no control characters. `ValueError` otherwise."""
    name = raw.strip()
    if not name:
        raise ValueError("family name must not be empty")
    if len(name) > FAMILY_NAME_MAX_CODEPOINTS:
        raise ValueError(f"family name exceeds {FAMILY_NAME_MAX_CODEPOINTS} characters")
    if wire.CONTROL_CHAR_RE.search(name):
        raise ValueError("family name contains control characters")
    return name


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


def update_family(
    family_id: str, *, name: str | None = None, settings: FamilySettingsPatch | None = None
) -> Family:
    """Patch-semantics update of `name` and `settings` (not `blockedNumbers`)."""
    updates: dict[str, object] = {}
    if name is not None:
        updates["name"] = name
    if settings is not None:
        updates.update(settings.dotted())
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


def is_frozen(family_id: str | None) -> bool:
    """True when `families/{family_id}.frozen` is set; a missing family is not frozen."""
    if not family_id:
        return False
    snap = _families().document(family_id).get()
    if not snap.exists:
        return False
    return bool((snap.to_dict() or {}).get("frozen"))


def set_frozen(family_id: str, frozen: bool) -> Family:
    ref = _families().document(family_id)
    try:
        ref.update({"frozen": frozen})
    except NotFound as exc:
        raise KeyError(f"no such family: {family_id!r}") from exc
    fetched = get_family(family_id)
    if fetched is None:
        raise KeyError(f"no such family: {family_id!r}")
    return fetched


def seed_for(family_id: str) -> str:
    """`idSeed or id` of `family_id`; the id itself for a family with no doc."""
    fam = get_family(family_id)
    return fam.seed if fam is not None else family_id


def user_family_frozen(uid: str) -> bool:
    """Whether `uid`'s family (`ownerFamilyId` for an external) is frozen."""
    from app.store import (
        users as users_store,  # users imports no families code; lazy to stay a leaf
    )

    user = users_store.get_user(uid)
    if user is None:
        return False
    return is_frozen(user.familyId or user.ownerFamilyId)
