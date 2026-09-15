
Write routes carry a per-uid rate limit (`RATE_LIMIT_ADMIN_WRITE_LIMIT` per window, default 60/min).
"""

from __future__ import annotations

import hashlib
import os
import secrets
from typing import Annotated, Literal

from firebase_admin import auth as fb_auth

from app.store import allow as allow_store
from app.store import backends as backends_store
from app.store import devices as devices_store
from app.store import rate_limits as rate_limits_store
from app.store import settings as settings_store
from app.store import users as users_store
from app.store.allow import AllowEdge, EdgeInput
from app.store.devices import Device
from app.store.settings import RetentionSetting, RetentionSettings


MQTT_PASSWORD_BYTES = 24

DEFAULT_ADMIN_WRITE_LIMIT = 60
DEFAULT_ADMIN_WRITE_WINDOW_S = 60


def _admin_write_rate_limit() -> tuple[int, int]:
    limit = int(os.environ.get("RATE_LIMIT_ADMIN_WRITE_LIMIT", str(DEFAULT_ADMIN_WRITE_LIMIT)))
    window_s = int(
        os.environ.get("RATE_LIMIT_ADMIN_WRITE_WINDOW_S", str(DEFAULT_ADMIN_WRITE_WINDOW_S))
    )
    return limit, window_s


def require_admin_write_rate_limit(
) -> None:
    limit, window_s = _admin_write_rate_limit()
    if not rate_limits_store.check_and_increment(
        f"admin:{authed.uid}", limit=limit, window_s=window_s
    ):
        raise HTTPException(status_code=429, detail="admin rate limit exceeded; try again later")


class CreateUserRequest(BaseModel):
    alias: str
    displayName: str
    email: str | None = None
    phone: str | None = None
    role: Literal["admin", "member"] = "member"
    uid: str | None = None  # optional: pin a specific uid (tests, re-runs)


class PatchUserRequest(BaseModel):
    displayName: str | None = None
    email: str | None = None
    phone: str | None = None
    disabled: bool | None = None


@router.post("/users", dependencies=[Depends(require_admin_write_rate_limit)])
        raise HTTPException(status_code=400, detail="email or phone is required")


    try:
        user = users_store.create_user(
            alias=req.alias,
            display_name=req.displayName,
            email=req.email,
            role=req.role,
        )
    except (users_store.AliasTaken, users_store.InvalidAlias) as exc:
        # roll back the Auth account so a rejected alias leaves no orphaned Auth user
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return user


@router.get("/users")
        raise HTTPException(status_code=404, detail="no such user")
    user = users_store.update_user(
        uid,
    )
    return user


@router.delete("/users/{uid}", dependencies=[Depends(require_admin_write_rate_limit)])
    if users_store.get_user(uid) is None:
        raise HTTPException(status_code=404, detail="no such user")
    try:
        fb_auth.delete_user(uid)
    except fb_auth.UserNotFoundError:
        pass
    return {"ok": True}




class AllowlistEntry(BaseModel):
    model_config = ConfigDict(extra="ignore")

    fromAlias: str
    toAlias: str
    message: bool = True
    locate: bool = True


class PutAllowlistRequest(BaseModel):
    entries: list[AllowlistEntry]


def _resolve_uid(alias: str) -> str:
    uid = users_store.get_uid_for_alias(alias)
    if uid is None:
        raise HTTPException(status_code=400, detail=f"unknown alias: {alias!r}")
    return uid


@router.put("/allowlist", dependencies=[Depends(require_admin_write_rate_limit)])
    edges = [
        EdgeInput(
            from_uid=_resolve_uid(e.fromAlias),
            to_uid=_resolve_uid(e.toAlias),
            message=e.message,
            locate=e.locate,
        )
        for e in req.entries
    ]


@router.get("/allowlist")
def get_allowlist() -> list[AllowEdge]:
    return allow_store.list_edges()


class CreateDeviceRequest(BaseModel):
    ownerAlias: str
    label: str
    defaultToAlias: str | None = None


    device: Device


    owner_uid = _resolve_uid(req.ownerAlias)
    default_to_uid = _resolve_uid(req.defaultToAlias) if req.defaultToAlias else None

    password = secrets.token_urlsafe(MQTT_PASSWORD_BYTES)
    password_hash = hashlib.sha256(password.encode("utf-8")).hexdigest()
        owner_uid=owner_uid,
        label=req.label,
        mqtt_password_hash=password_hash,
        default_to_uid=default_to_uid,
    )
    # the pager is a delivery backend of its owner; without this it is undeliverable
    backends_store.create_backend(
    )
    # pick up incoming `locate` edges that predate this device
    allow_store.recompute_locatable_by_for_owner(owner_uid)
    assert device is not None


@router.get("/devices")


        raise HTTPException(status_code=404, detail="no such device")
    return {"ok": True}


    device = devices_store.get_device(device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="no such device")
    password = secrets.token_urlsafe(MQTT_PASSWORD_BYTES)
    password_hash = hashlib.sha256(password.encode("utf-8")).hexdigest()


class PutSettingsRequest(BaseModel):


@router.put("/settings", dependencies=[Depends(require_admin_write_rate_limit)])
def put_settings(req: PutSettingsRequest) -> RetentionSettings:


@router.get("/settings")
def get_settings() -> RetentionSettings:
    return settings_store.get_retention()
