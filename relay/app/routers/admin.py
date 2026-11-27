"""`/api/admin/*`: super-only user, allow-list and device administration.

Write routes carry a per-uid rate limit (`RATE_LIMIT_ADMIN_WRITE_LIMIT` per window, default 60/min).
"""

from __future__ import annotations

import hashlib
import logging
import os
import secrets
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request
from firebase_admin import auth as fb_auth
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app import (
    apn_presets,
    authn_home,
    book,
    ca_resolve,
    devcfg,
    devsetup,
    firmware,
    move_target,
    purge,
    soracom,
)
from app.auth import AuthedUser, principal_for, require_super, set_claims
from app.broker import BrokerClient
from app.broker_admin import BrokerAdmin, EmqxResult, make_broker_admin
from app.config import Settings, auth_backend
from app.db.firestore import get_db
from app.device_ids import DEVICE_ID_ATTEMPTS, new_device_id
from app.fed import linking as fed_linking
from app.ingest import Ingest
from app.store import allow as allow_store
from app.store import backends as backends_store
from app.store import contacts as contacts_store
from app.store import device_secrets as device_secrets_store
from app.store import devices as devices_store
from app.store import externals as externals_store
from app.store import families as families_store
from app.store import messages as messages_store
from app.store import rate_limits as rate_limits_store
from app.store import settings as settings_store
from app.store import users as users_store
from app.store.allow import AllowEdge, EdgeInput
from app.store.devices import Device
from app.store.families import Family
from app.store.settings import RetentionSetting, RetentionSettings
from app.store.users import User

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin", dependencies=[Depends(require_super)])

MQTT_PASSWORD_BYTES = 24
LABEL_MAX_BYTES = 32

DEFAULT_ADMIN_WRITE_LIMIT = 60
DEFAULT_ADMIN_WRITE_WINDOW_S = 60


def _admin_write_rate_limit() -> tuple[int, int]:
    limit = int(os.environ.get("RATE_LIMIT_ADMIN_WRITE_LIMIT", str(DEFAULT_ADMIN_WRITE_LIMIT)))
    window_s = int(
        os.environ.get("RATE_LIMIT_ADMIN_WRITE_WINDOW_S", str(DEFAULT_ADMIN_WRITE_WINDOW_S))
    )
    return limit, window_s


def get_broker(request: Request) -> BrokerClient:
    """The app's broker client."""
    return request.app.state.broker


def get_ingest(request: Request) -> Ingest:
    """The app's `Ingest`, so a rotated `hmacKey` invalidates the cache that verifies envelopes."""
    return request.app.state.ingest


def get_emqx(request: Request) -> BrokerAdmin:
    """`app.state.emqx_admin` if set (tests inject one), else a fresh `make_broker_admin`."""
    existing = getattr(request.app.state, "emqx_admin", None)
    if existing is not None:
        return existing
    return make_broker_admin(request.app.state.settings, request.app.state.broker)


def get_app_settings(request: Request) -> Settings:
    return request.app.state.settings


def require_admin_write_rate_limit(
    authed: Annotated[AuthedUser, Depends(require_super)],
) -> None:
    """Adds the rate-limit check; FastAPI caches the dependency, so the token is verified once."""
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
    # super may pin a new user's family; unset defaults to the calling super's own family
    familyId: str | None = None
    uid: str | None = None  # optional: pin a specific uid (tests, re-runs)


class PatchUserRequest(BaseModel):
    displayName: str | None = None
    email: str | None = None
    phone: str | None = None
    # super may also promote/demote to `super`
    role: Literal["super", "admin", "member"] | None = None
    # Moves a user between families; absent and `null` both mean "leave familyId alone".
    familyId: str | None = None
    disabled: bool | None = None


@router.post("/users", dependencies=[Depends(require_admin_write_rate_limit)])
def create_user(
    req: CreateUserRequest, authed: Annotated[AuthedUser, Depends(require_super)]
) -> User:
    home = auth_backend() == "home"
    # at home the alias is the login name; email/phone are optional
    if not home and not req.email and not req.phone:
        raise HTTPException(status_code=400, detail="email or phone is required")

    # an omitted `familyId` defaults to the calling super's own family
    family_id = req.familyId if req.familyId is not None else principal_for(authed).family_id
    if family_id is None:
        raise HTTPException(status_code=400, detail="familyId is required")

    # sign-in number only, never an SMS route
    phone = req.phone
    if phone:
        try:
            phone = externals_store.normalize_phone(phone)
        except ValueError as exc:
            raise HTTPException(
                status_code=400, detail="phone must be a number like +12065550100"
            ) from exc

    if home:
        uid = req.uid or authn_home.new_uid()
    else:
        kwargs: dict[str, object] = {}
        if req.uid:
            kwargs["uid"] = req.uid
        if req.email:
            kwargs["email"] = req.email
        if phone:
            kwargs["phone_number"] = phone
        try:
            uid = fb_auth.create_user(**kwargs).uid
        except fb_auth.EmailAlreadyExistsError as exc:
            raise HTTPException(status_code=409, detail="email already registered") from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        user = users_store.create_user(
            uid=uid,
            alias=req.alias,
            display_name=req.displayName,
            email=req.email,
            phone=phone,
            role=req.role,
            family_id=family_id,
        )
    except (users_store.AliasTaken, users_store.InvalidAlias) as exc:
        # roll back the Auth account so a rejected alias leaves no orphaned Auth user
        if not home:
            fb_auth.delete_user(uid)
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # claims are the only source of truth for `role`/`fam`
    set_claims(user.uid, user.role, user.familyId)
    return user


@router.get("/users")
def list_users(family: Annotated[str | None, Query()] = None) -> list[User]:
    """`?family=` narrows the listing to one family (filtered in Python)."""
    users = users_store.list_users()
    if family is not None:
        users = [u for u in users if u.familyId == family]
    return users


def _patch_user_impl(
    uid: str,
    *,
    display_name: str | None,
    email: str | None,
    phone: str | None,
    role: str | None,
    family_id: str | None,
    disabled: bool | None,
    broker: BrokerClient,
) -> User:
    """Shared by the admin and family PATCH routes: patch, re-issue claims, bump affected books."""
    existing = users_store.get_user(uid)
    if existing is None:
        raise HTTPException(status_code=404, detail="no such user")
    if family_id is not None:
        # no store setter for `familyId`: write the field directly
        get_db().collection("users").document(uid).update({"familyId": family_id})
    user = users_store.update_user(
        uid,
        display_name=display_name,
        email=email,
        phone=phone,
        role=role,
        disabled=disabled,
    )
    if role is not None or family_id is not None:
        set_claims(uid, user.role, user.familyId)
    # a `displayName`, `disabled` or `familyId` change alters every book listing this uid
    name_changed = display_name is not None and display_name != existing.displayName
    disabled_changed = disabled is not None and disabled != existing.disabled
    family_changed = family_id is not None and family_id != existing.familyId
    if name_changed or disabled_changed or family_changed:
        owners = (
            book.edge_holders(uid)
            | book.family_uids(existing.familyId)
            | book.family_uids(user.familyId)
            | {uid}
        )
        book.bump_and_push(owners, broker, reason="member_patch")
    if family_changed:
        # the owner's family contacts (and so `cfg.sms`) changed
        book.rederive_sms_contacts(uid, broker)
    # a displayName change rewrites `participants` in every conversation of this uid
    if name_changed:
        messages_store.rewrite_participants_for_user(uid)
    return user


@router.patch("/users/{uid}", dependencies=[Depends(require_admin_write_rate_limit)])
def patch_user(
    uid: str, req: PatchUserRequest, broker: Annotated[BrokerClient, Depends(get_broker)]
) -> User:
    return _patch_user_impl(
        uid,
        display_name=req.displayName,
        email=req.email,
        phone=req.phone,
        role=req.role,
        family_id=req.familyId,
        disabled=req.disabled,
        broker=broker,
    )


@router.delete("/users/{uid}", dependencies=[Depends(require_admin_write_rate_limit)])
def delete_user(
    uid: str, emqx: Annotated[BrokerAdmin, Depends(get_emqx)]
) -> dict[str, bool]:
    """Cascade via `app.purge.purge_user`: everything naming the uid."""
    if users_store.get_user(uid) is None:
        raise HTTPException(status_code=404, detail="no such user")
    purge.purge_user(uid, emqx)
    if auth_backend() == "home":
        authn_home.delete_account(uid)
        return {"ok": True}
    try:
        fb_auth.delete_user(uid)
    except fb_auth.UserNotFoundError:
        pass
    return {"ok": True}


class SetPasswordRequest(BaseModel):
    password: str = Field(min_length=8, max_length=256)


@router.post("/users/{uid}/password", dependencies=[Depends(require_admin_write_rate_limit)])
def set_user_password(uid: str, req: SetPasswordRequest) -> dict[str, bool]:
    """Set or reset a member's password (home only)."""
    if auth_backend() != "home":
        raise HTTPException(status_code=404, detail="not found")
    if users_store.get_user(uid) is None:
        raise HTTPException(status_code=404, detail="no such user")
    authn_home.set_password(uid, req.password)
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


def _message_sets_by_owner(edges: list[AllowEdge]) -> dict[str, frozenset[str]]:
    """Per owner, the `toUid` set with `message=True` (what fixes a book's `c[]`), for diffing."""
    by_owner: dict[str, set[str]] = {}
    for e in edges:
        if e.message:
            by_owner.setdefault(e.fromUid, set()).add(e.toUid)
    return {uid: frozenset(to_uids) for uid, to_uids in by_owner.items()}


@router.put("/allowlist", dependencies=[Depends(require_admin_write_rate_limit)])
def put_allowlist(
    req: PutAllowlistRequest,
    broker: Annotated[BrokerClient, Depends(get_broker)],
    # `?family=` narrows which existing edges the replace touches; still replace-all and super-only
    family: Annotated[str | None, Query()] = None,
) -> list[AllowEdge]:
    edges = [
        EdgeInput(
            from_uid=_resolve_uid(e.fromAlias),
            to_uid=_resolve_uid(e.toAlias),
            message=e.message,
            locate=e.locate,
        )
        for e in req.entries
    ]
    # snapshot before `replace_all` so the diff is against the pre-request state (PROTOCOL.md §3.7)
    old_edges = allow_store.list_edges()
    old_message_sets = _message_sets_by_owner(old_edges)
    try:
        result = allow_store.replace_all(edges, family_id=family)
    except allow_store.LocateCrossFamily as exc:
        # a `locate: true` edge whose ends do not share a non-null `familyId` is refused
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    new_message_sets = _message_sets_by_owner(result)
    changed_owner_uids = {
        owner_uid
        for owner_uid in old_message_sets.keys() | new_message_sets.keys()
        if old_message_sets.get(owner_uid, frozenset())
        != new_message_sets.get(owner_uid, frozenset())
    }
    for owner_uid in changed_owner_uids:
        for device in devices_store.list_devices(owner_uid=owner_uid):
            contacts_store.bump_book_version(device.id)
            devcfg.push_book(device.id, broker)
    # `cfg.sms` is an edge projection: re-derive it for every touched owner
    for owner_uid in sorted({e.fromUid for e in old_edges} | {e.fromUid for e in result}):
        book.rederive_sms_contacts(owner_uid, broker)
    return result


@router.get("/allowlist")
def get_allowlist() -> list[AllowEdge]:
    return allow_store.list_edges()


class CreateDeviceRequest(BaseModel):
    """No `deviceId`: the relay issues it; a stale client's value is ignored."""

    ownerAlias: str
    label: str
    defaultToAlias: str | None = None
    # Carrier APN; omitted/empty = carrier default.
    apn: str | None = None


class SetApnRequest(BaseModel):
    apn: str | None = None


class DeviceSetupCodeResponse(BaseModel):
    """Response for device create and rotate-credentials; plaintext secrets leave only in the bundle."""

    device: Device
    setupCode: str
    expiresAt: datetime
    # "pushed": the broker accepted the login and ACL; "manual": it did not, and `manualAcl` lists
    # what to configure by hand. `brokerResult` says why: "ok", "manual" (the relay does not manage
    # the broker) or "error" (it does, and the broker refused or did not answer; rotate again).
    brokerPush: Literal["pushed", "manual"]
    brokerResult: Literal["ok", "manual", "error"]
    manualAcl: list[str] | None = None


HMAC_KEY_BYTES = 32


def _bootstrap_host_and_ca(settings: Settings) -> tuple[str, str | None]:
    """The broker host for the bootstrap bundle and the CA PEM to pin (settings win)."""
    ca = settings.broker_ca_pem or ca_resolve.get_broker_ca_pem()
    return settings.broker_host, ca


# Bit 0 of the bundle `flags` is `req_sig`; it must track `authMode`, else an `hmac` device
# publishes unsigned and `ingest.py` drops every envelope as `bad-sig`.
_FLAG_REQ_SIG = 1


def _bootstrap_flags(auth_mode: str) -> int:
    return _FLAG_REQ_SIG if auth_mode == "hmac" else 0


def _manual_acl_lines(device_id: str) -> list[str]:
    """Display-only mirror of the broker ACL, shown when the real push did not happen."""
    return [
        f"publish pager/{device_id}/up",
        f"publish pager/{device_id}/status",
        f"publish pager/{device_id}/loc",
        f"subscribe pager/{device_id}/down",
    ]


def _broker_result(login: EmqxResult, issued: devsetup.IssueOutcome) -> EmqxResult:
    """One answer for the pushes of a setup code: the device login, the bootstrap login and the
    retained bundle."""
    if login != "ok":
        return login
    if issued.boot_user != "ok":
        return issued.boot_user
    return "ok" if issued.bundle_published else "error"


def _broker_push_outcome(
    device_id: str, result: EmqxResult
) -> tuple[Literal["pushed", "manual"], list[str] | None]:
    if result == "ok":
        return "pushed", None
    return "manual", _manual_acl_lines(device_id)


def _issue_device_id() -> str:
    """A fresh `pgr-xxxxxxxx` id with no `devices/` or `deviceSecrets/` doc."""
    for _ in range(DEVICE_ID_ATTEMPTS):
        candidate = new_device_id()
        if (
            devices_store.get_device(candidate) is None
            and device_secrets_store.get(candidate) is None
        ):
            return candidate
    raise HTTPException(status_code=500, detail="could not allocate a device id")


def _create_device_impl(
    req: CreateDeviceRequest,
    broker: BrokerClient,
    emqx: BrokerAdmin,
    settings: Settings,
) -> DeviceSetupCodeResponse:
    """The setup-code flow shared with `POST /api/family/devices`, after its own scope check."""
    owner_uid = _resolve_uid(req.ownerAlias)
    owner = users_store.get_user(owner_uid)
    default_to_uid = _resolve_uid(req.defaultToAlias) if req.defaultToAlias else None
    if default_to_uid is not None:
        default_to = users_store.get_user(default_to_uid)
        if default_to is not None and default_to.kind == "external":
            raise HTTPException(
                status_code=400, detail="the default recipient cannot be an SMS contact"
            )
    try:
        apn = apn_presets.validate_apn(req.apn)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    device_id = _issue_device_id()

    # generate the MQTT password and HMAC key; the plaintext exists only here
    password = secrets.token_urlsafe(MQTT_PASSWORD_BYTES)
    password_hash = hashlib.sha256(password.encode("utf-8")).hexdigest()
    hmac_key = secrets.token_bytes(HMAC_KEY_BYTES)

    devices_store.create_device(
        device_id=device_id,
        owner_uid=owner_uid,
        label=req.label,
        # a device's family is copied from its owner
        family_id=owner.familyId if owner is not None else None,
        mqtt_username=device_id,
        mqtt_password_hash=password_hash,
        default_to_uid=default_to_uid,
        apn=apn,
    )
    device_secrets_store.create(device_id, hmac_key=hmac_key, mqtt_password_hash=password_hash)
    # the pager is a delivery backend of its owner; without this it is undeliverable
    backends_store.create_backend(
        owner_uid, kind="pager", config={"deviceId": device_id}, enabled=True
    )
    # pick up incoming `locate` edges that predate this device
    allow_store.recompute_locatable_by_for_owner(owner_uid)

    # PROTOCOL.md §3.7: device creation bumps and pushes the new device's book. Best-effort.
    contacts_store.bump_book_version(device_id)
    devcfg.push_book(device_id, broker)
    # seed `cfg.sms` (implied contacts for an open owner)
    book.rederive_sms_contacts(owner_uid, broker)

    # push the broker credential and ACL before handing out a setup code
    emqx_result = emqx.ensure_device(device_id, password, device_id)
    issued = devsetup.IssueOutcome()
    host, ca = _bootstrap_host_and_ca(settings)
    now = datetime.now(UTC)
    try:
        setup_code = devsetup.issue(
            device_id,
            mqtt_password=password,
            hmac_key=hmac_key,
            host=host,
            ca_pem=ca,
            flags=_bootstrap_flags("hmac"),
            label=req.label,
            apn=apn,
            settings=settings,
            broker=broker,
            emqx=emqx,
            now=now,
            outcome=issued,
        )
    except ca_resolve.PublicBaseUrlRequired as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    expires_at = now + timedelta(minutes=devsetup.EXPIRY_MINUTES)

    device = devices_store.get_device(device_id)
    assert device is not None
    emqx_result = _broker_result(emqx_result, issued)
    broker_push, manual_acl = _broker_push_outcome(device_id, emqx_result)
    return DeviceSetupCodeResponse(
        device=device,
        setupCode=setup_code,
        expiresAt=expires_at,
        brokerPush=broker_push,
        brokerResult=emqx_result,
        manualAcl=manual_acl,
    )


@router.post("/devices", dependencies=[Depends(require_admin_write_rate_limit)])
def create_device(
    req: CreateDeviceRequest,
    broker: Annotated[BrokerClient, Depends(get_broker)],
    emqx: Annotated[BrokerAdmin, Depends(get_emqx)],
    settings: Annotated[Settings, Depends(get_app_settings)],
) -> DeviceSetupCodeResponse:
    return _create_device_impl(req, broker, emqx, settings)


class PatchDeviceRequest(BaseModel):
    label: str

    @field_validator("label")
    @classmethod
    def _check_label(cls, v: str) -> str:
        v = v.strip()
        # the pager's label buffer is 32 bytes
        if not 1 <= len(v.encode("utf-8")) <= LABEL_MAX_BYTES:
            raise ValueError(f"label must be 1..{LABEL_MAX_BYTES} bytes of UTF-8")
        return v


def _patch_device_impl(device_id: str, req: PatchDeviceRequest) -> Device:
    """Rename a device; the pager learns it from the next `rotate-credentials` bundle."""
    if devices_store.get_device(device_id) is None:
        raise HTTPException(status_code=404, detail="no such device")
    devices_store.set_label(device_id, req.label)
    device = devices_store.get_device(device_id)
    assert device is not None
    return device


@router.patch("/devices/{device_id}", dependencies=[Depends(require_admin_write_rate_limit)])
def patch_device(device_id: str, req: PatchDeviceRequest) -> Device:
    """Edit the free-text `label`; the pager shows it after the next `rotate-credentials`."""
    return _patch_device_impl(device_id, req)


@router.get("/devices")
def list_devices(family: Annotated[str | None, Query()] = None) -> list[Device]:
    """`?family=` narrows the listing to one family."""
    devices = devices_store.list_devices()
    if family is not None:
        devices = [d for d in devices if d.familyId == family]
    return devices


def _delete_device_impl(device_id: str, emqx: BrokerAdmin) -> dict[str, bool]:
    """Cascade via `app.purge.purge_device`: the device, its rows, backend row and credential."""
    if devices_store.get_device(device_id) is None:
        raise HTTPException(status_code=404, detail="no such device")
    purge.purge_device(device_id, emqx)
    return {"ok": True}


@router.delete("/devices/{device_id}", dependencies=[Depends(require_admin_write_rate_limit)])
def delete_device(
    device_id: str, emqx: Annotated[BrokerAdmin, Depends(get_emqx)]
) -> dict[str, bool]:
    return _delete_device_impl(device_id, emqx)


@router.delete("/families/{family_id}", dependencies=[Depends(require_admin_write_rate_limit)])
def delete_family(
    family_id: str, emqx: Annotated[BrokerAdmin, Depends(get_emqx)]
) -> dict[str, object]:
    """Super only; the family must be frozen or it is a 409."""
    snap = get_db().collection("families").document(family_id).get()
    if not snap.exists:
        raise HTTPException(status_code=404, detail="no such family")
    if not purge.is_frozen(family_id):
        raise HTTPException(status_code=409, detail="family must be frozen before it is deleted")
    # linked families are told (best effort) before the rows go
    fed_linking.unlink_family(family_id)
    report = purge.purge_family(family_id, emqx)
    return {"ok": True, "deleted": dict(report.deleted), "broker": report.broker}


def _rotate_credentials_impl(
    device_id: str,
    broker: BrokerClient,
    emqx: BrokerAdmin,
    settings: Settings,
    ingest: Ingest,
) -> DeviceSetupCodeResponse:
    device = devices_store.get_device(device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="no such device")

    password = secrets.token_urlsafe(MQTT_PASSWORD_BYTES)
    password_hash = hashlib.sha256(password.encode("utf-8")).hexdigest()
    hmac_key = secrets.token_bytes(HMAC_KEY_BYTES)

    # delete the old broker credential outright before `deviceSecrets.rotate` replaces key/hash
    emqx.delete_user(device.mqttUsername)
    if device_secrets_store.get(device_id) is None:
        # a pager imported without a key (a move could not derive one)
        device_secrets_store.create(device_id, hmac_key=hmac_key, mqtt_password_hash=password_hash)
    else:
        device_secrets_store.rotate(device_id, hmac_key, password_hash)
    move_target.end_for_setup_code(device_id)
    # the alarm and its window concern the old key
    devices_store.clear_auth_alarm(device_id)
    # drop the cached secret so envelopes are not verified against the deleted `hmacKey`
    ingest.invalidate_secret_cache(device_id)
    devices_store.set_provision_state(device_id, "issued")

    emqx_result = emqx.ensure_device(device.mqttUsername, password, device_id)
    issued = devsetup.IssueOutcome()
    host, ca = _bootstrap_host_and_ca(settings)
    now = datetime.now(UTC)
    try:
        setup_code = devsetup.issue(
            device_id,
            mqtt_password=password,
            hmac_key=hmac_key,
            host=host,
            ca_pem=ca,
            flags=_bootstrap_flags(device.authMode),
            label=device.label,
            apn=device.apn,
            settings=settings,
            broker=broker,
            emqx=emqx,
            now=now,
            outcome=issued,
        )
    except ca_resolve.PublicBaseUrlRequired as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    expires_at = now + timedelta(minutes=devsetup.EXPIRY_MINUTES)

    updated = devices_store.get_device(device_id)
    assert updated is not None
    emqx_result = _broker_result(emqx_result, issued)
    broker_push, manual_acl = _broker_push_outcome(device_id, emqx_result)
    return DeviceSetupCodeResponse(
        device=updated,
        setupCode=setup_code,
        expiresAt=expires_at,
        brokerPush=broker_push,
        brokerResult=emqx_result,
        manualAcl=manual_acl,
    )


@router.post(
    "/devices/{device_id}/rotate-credentials",
    dependencies=[Depends(require_admin_write_rate_limit)],
)
def rotate_credentials(
    device_id: str,
    broker: Annotated[BrokerClient, Depends(get_broker)],
    emqx: Annotated[BrokerAdmin, Depends(get_emqx)],
    settings: Annotated[Settings, Depends(get_app_settings)],
    ingest: Annotated[Ingest, Depends(get_ingest)],
) -> DeviceSetupCodeResponse:
    return _rotate_credentials_impl(device_id, broker, emqx, settings, ingest)


def _revoke_device_impl(device_id: str, emqx: BrokerAdmin) -> Device:
    """Revoke the device and delete its broker credential."""
    device = devices_store.get_device(device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="no such device")
    emqx.delete_user(device.mqttUsername)
    return devices_store.revoke_device(device_id)


@router.post(
    "/devices/{device_id}/revoke", dependencies=[Depends(require_admin_write_rate_limit)]
)
def revoke_device(
    device_id: str,
    emqx: Annotated[BrokerAdmin, Depends(get_emqx)],
) -> Device:
    return _revoke_device_impl(device_id, emqx)


class LockCfg(BaseModel):
    model_config = ConfigDict(extra="ignore")

    clear: bool | None = None
    # stored device-side as a `u8` (0-255 minutes; 0 = never)
    auto: int | None = Field(default=None, ge=0, le=255)


class PushCfgRequest(BaseModel):
    lock: LockCfg


def _push_cfg_impl(device_id: str, req: PushCfgRequest, broker: BrokerClient) -> dict[str, bool]:
    if devices_store.get_device(device_id) is None:
        raise HTTPException(status_code=404, detail="no such device")
    lock = {k: v for k, v in req.lock.model_dump().items() if v is not None}
    ok = devcfg.push_cfg(device_id, lock, broker)
    return {"ok": ok}


@router.post("/devices/{device_id}/cfg", dependencies=[Depends(require_admin_write_rate_limit)])
def push_cfg(
    device_id: str,
    req: PushCfgRequest,
    broker: Annotated[BrokerClient, Depends(get_broker)],
) -> dict[str, bool]:
    return _push_cfg_impl(device_id, req, broker)


class PushCaRequest(BaseModel):
    action: Literal["push", "unpin"]


def _list_apn_presets_impl() -> list[apn_presets.ApnPreset]:
    return apn_presets.PRESETS


@router.get("/apn-presets")
def list_apn_presets() -> list[apn_presets.ApnPreset]:
    """Carrier APN choices for the device forms."""
    return _list_apn_presets_impl()


def _set_device_apn_impl(device_id: str, req: SetApnRequest) -> dict[str, str | None]:
    if devices_store.get_device(device_id) is None:
        raise HTTPException(status_code=404, detail="no such device")
    try:
        apn = apn_presets.validate_apn(req.apn)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    devices_store.set_apn(device_id, apn)
    return {"apn": apn}


@router.put("/devices/{device_id}/apn", dependencies=[Depends(require_admin_write_rate_limit)])
def set_device_apn(device_id: str, req: SetApnRequest) -> dict[str, str | None]:
    """Store the carrier APN for setup codes and bundles; nothing is pushed to the pager."""
    return _set_device_apn_impl(device_id, req)


def _push_ca_impl(
    device_id: str, req: PushCaRequest, broker: BrokerClient, settings: Settings
) -> dict[str, bool]:
    """`push` sends the relay's CA as a `/down cfg.ca` pointer; `unpin` sends `cfg.ca = {url: ""}`."""
    if devices_store.get_device(device_id) is None:
        raise HTTPException(status_code=404, detail="no such device")
    if req.action == "unpin":
        ok = devcfg.unpin_ca(device_id, broker)
        return {"ok": ok}
    ca_pem = settings.broker_ca_pem or ca_resolve.get_broker_ca_pem()
    if not ca_pem:
        raise HTTPException(
            status_code=400, detail="no CA is configured on this relay to push"
        )
    try:
        ok = devcfg.push_ca(device_id, pem=ca_pem, broker=broker, settings=settings)
    except ca_resolve.PublicBaseUrlRequired as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {"ok": ok}


@router.post("/devices/{device_id}/ca", dependencies=[Depends(require_admin_write_rate_limit)])
def push_ca(
    device_id: str,
    req: PushCaRequest,
    broker: Annotated[BrokerClient, Depends(get_broker)],
    settings: Annotated[Settings, Depends(get_app_settings)],
) -> dict[str, bool]:
    return _push_ca_impl(device_id, req, broker, settings)


class OtaRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target: str | None = Field(default=None, pattern=r"^[0-9a-f]{16}$")
    cancel: bool | None = None


_JOB_BUSY = "another update is in progress"
_AST_UNSUPPORTED = "needs firmware with font OTA and the new partition table"


class AssetsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target: str | None = Field(default=None, pattern=r"^[0-9a-f]{16}$")
    cancel: bool | None = None


def _load_assets_index(settings: Settings) -> firmware.AssetsIndex:
    try:
        return firmware.load_assets_index(settings)
    except firmware.FirmwareNotConfigured:
        raise HTTPException(status_code=503, detail="OTA not configured") from None
    except firmware.FirmwareIndexUnavailable:
        raise HTTPException(status_code=502, detail="assets index unavailable") from None


def _assets_blocked(dev: Device) -> str | None:
    """The 409 reason for a font push to `dev`, or None."""
    if dev.status.assetsSrc not in (1, 2):
        return _AST_UNSUPPORTED
    if dev.otaJob is not None:
        return _JOB_BUSY
    return None


def _list_assets_impl(settings: Settings, device: str | None) -> dict[str, object]:
    index = _load_assets_index(settings)
    active: str | None = None
    blocked: str | None = None
    if device is not None:
        dev = devices_store.get_device(device)
        if dev is None:
            raise HTTPException(status_code=404, detail="no such device")
        active = dev.status.assetsId if dev.status.assetsSrc == 2 else None
        blocked = _assets_blocked(dev)
    builds: list[dict[str, object]] = []
    for b in sorted(index.builds, key=lambda b: b.published, reverse=True):
        builds.append(
            {
                "id": b.id,
                "id16": b.id16,
                "label": b.label,
                "lang": b.lang,
                "fv": b.fv,
                "size": b.size,
                "published": b.published,
                "full": {"path": b.full.path, "osz": b.full.osz, "osha": b.full.osha},
                "osz": b.full.osz,
                "estBytes": firmware.estimate_bytes(b.full.osz),
                "active": active is not None and active == b.id16,
            }
        )
    return {"builds": builds, "blocked": blocked}


def _push_assets_impl(
    device_id: str,
    req: AssetsRequest,
    by_uid: str,
    broker: BrokerClient,
    settings: Settings,
) -> dict[str, object]:
    if (req.target is None) == (not req.cancel):
        raise HTTPException(status_code=422, detail="send exactly one of target or cancel")
    dev = devices_store.get_device(device_id)
    if dev is None:
        raise HTTPException(status_code=404, detail="no such device")
    if not settings.fw_bucket_base:
        raise HTTPException(status_code=503, detail="OTA not configured")
    if req.cancel:
        if dev.otaJob is not None and dev.otaJob.part != "assets":
            # the device would only ack it; keep the app job's record
            raise HTTPException(status_code=409, detail=_JOB_BUSY)
        return {"ok": devcfg.cancel_ast(device_id, broker)}
    assert req.target is not None
    index = _load_assets_index(settings)
    build = index.find(req.target)
    if build is None:
        raise HTTPException(status_code=404, detail="no such font build")
    blocked = _assets_blocked(dev)
    if blocked is not None:
        raise HTTPException(status_code=409, detail=blocked)
    if dev.status.assetsSrc == 2 and dev.status.assetsId == build.id16:
        raise HTTPException(status_code=409, detail="already running that font image")
    est = firmware.estimate_bytes(build.full.osz)
    ok = devcfg.push_ast(
        device_id,
        firmware.build_cfg_ast(build, settings),
        broker,
        job={
            "target16": build.id16,
            "kind": "full",
            "osz": build.full.osz,
            "estBytes": est,
            "onDemand": False,
            "by_uid": by_uid,
        },
    )
    return {"ok": ok, "kind": "full", "osz": build.full.osz, "estBytes": est}


@router.get("/assets")
def list_assets(
    settings: Annotated[Settings, Depends(get_app_settings)],
    device: str | None = Query(default=None),
) -> dict[str, object]:
    """Published font images, newest first; with `device`, marks the active one."""
    return _list_assets_impl(settings, device)


@router.post("/devices/{device_id}/assets", dependencies=[Depends(require_admin_write_rate_limit)])
def push_assets(
    device_id: str,
    req: AssetsRequest,
    authed: Annotated[AuthedUser, Depends(require_super)],
    broker: Annotated[BrokerClient, Depends(get_broker)],
    settings: Annotated[Settings, Depends(get_app_settings)],
) -> dict[str, object]:
    """`{"target": "<16 hex>"}` pushes `cfg.ast` for that image; `{"cancel": true}` cancels."""
    return _push_assets_impl(device_id, req, authed.uid, broker, settings)


def _load_firmware_index(settings: Settings) -> firmware.FirmwareIndex:
    try:
        return firmware.load_index(settings)
    except firmware.FirmwareNotConfigured:
        raise HTTPException(status_code=503, detail="OTA not configured") from None
    except firmware.FirmwareIndexUnavailable:
        raise HTTPException(status_code=502, detail="firmware index unavailable") from None


def _list_firmware_impl(
    settings: Settings, device: str | None
) -> dict[str, list[dict[str, object]]]:
    index = _load_firmware_index(settings)
    img: str | None = None
    if device is not None:
        dev = devices_store.get_device(device)
        if dev is None:
            raise HTTPException(status_code=404, detail="no such device")
        img = dev.status.img
    builds: list[dict[str, object]] = []
    for b in sorted(index.builds, key=lambda b: b.published, reverse=True):
        c = firmware.choose(index, b.id16, img)
        entry: dict[str, object] = {
            "id16": b.id16,
            "version": b.version,
            "size": b.size,
            "published": b.published,
        }
        on_demand = False
        if c.kind == "full" and img is not None:
            img16 = img.strip().lower()[:16]
            cached = firmware.cached_delta(index, img16, b.id16, settings)
            if cached is not None:
                c = firmware.Choice("delta", b, cached, on_demand=True)
            else:
                on_demand = firmware.can_delta_on_demand(index, img16, b.id16)
        entry.update(kind=c.kind, osz=c.obj.osz, estBytes=firmware.estimate_bytes(c.obj.osz))
        if on_demand:
            entry["onDemandDelta"] = True
        builds.append(entry)
    return {"builds": builds}


@router.get("/firmware")
def list_firmware(
    settings: Annotated[Settings, Depends(get_app_settings)],
    device: str | None = Query(default=None),
) -> dict[str, list[dict[str, object]]]:
    """Published builds, newest first, each with the object this device would get."""
    return _list_firmware_impl(settings, device)


def _push_ota_impl(
    device_id: str,
    req: OtaRequest,
    by_uid: str,
    broker: BrokerClient,
    settings: Settings,
) -> dict[str, object]:
    if (req.target is None) == (not req.cancel):
        raise HTTPException(status_code=422, detail="send exactly one of target or cancel")
    dev = devices_store.get_device(device_id)
    if dev is None:
        raise HTTPException(status_code=404, detail="no such device")
    if not (settings.fw_index_url and settings.fw_bucket_base):
        raise HTTPException(status_code=503, detail="OTA not configured")
    if dev.otaJob is not None and dev.otaJob.part == "assets":
        # one job at a time; the device would only ack an app cancel while a font job is live
        raise HTTPException(status_code=409, detail=_JOB_BUSY)
    if req.cancel:
        return {"ok": devcfg.cancel_ota(device_id, broker)}
    assert req.target is not None
    index = _load_firmware_index(settings)
    if index.find(req.target) is None:
        raise HTTPException(status_code=404, detail="no such firmware build")
    if dev.status.otaCap != 1:
        raise HTTPException(status_code=409, detail="device does not report ota:1")
    target_build = index.find(req.target)
    assert target_build is not None
    refusal = firmware.move_refusal(
        target_build, move_state=dev.move.state if dev.move else None
    )
    if refusal is not None:
        raise HTTPException(status_code=422, detail=refusal)
    if dev.status.img and dev.status.img.strip().lower()[:16] == target_build.id16:
        raise HTTPException(status_code=409, detail="already running that build")
    choice = firmware.choose(index, req.target, dev.status.img)
    if choice.kind == "full" and dev.status.img:
        img16 = dev.status.img.strip().lower()[:16]
        if firmware.can_delta_on_demand(index, img16, target_build.id16):
            made = firmware.delta_on_demand(index, img16, target_build.id16, settings)
            if made is not None:
                choice = firmware.Choice("delta", target_build, made, on_demand=True)
    est = firmware.estimate_bytes(choice.obj.osz)
    ok = devcfg.push_ota(
        device_id,
        firmware.build_cfg_ota(choice, settings),
        broker,
        job={
            "target16": req.target,
            "kind": choice.kind,
            "osz": choice.obj.osz,
            "estBytes": est,
            "onDemand": choice.on_demand,
            "by_uid": by_uid,
        },
    )
    resp: dict[str, object] = {
        "ok": ok,
        "kind": choice.kind,
        "osz": choice.obj.osz,
        "estBytes": est,
    }
    if choice.on_demand:
        resp["onDemand"] = True
    return resp


@router.post("/devices/{device_id}/ota", dependencies=[Depends(require_admin_write_rate_limit)])
def push_ota(
    device_id: str,
    req: OtaRequest,
    authed: Annotated[AuthedUser, Depends(require_super)],
    broker: Annotated[BrokerClient, Depends(get_broker)],
    settings: Annotated[Settings, Depends(get_app_settings)],
) -> dict[str, object]:
    """`{"target": "<16 hex>"}` pushes `cfg.ota` (delta if `img` matches a base); `{"cancel": true}` cancels."""
    return _push_ota_impl(device_id, req, authed.uid, broker, settings)


class PutSettingsRequest(BaseModel):
    """Every field optional (merge write); out-of-bounds is a 422; legacy `{n, unit}` becomes days."""

    model_config = ConfigDict(extra="forbid")

    messagesDays: int | None = Field(default=None, ge=1, le=365)
    locationsDays: int | None = Field(default=None, ge=1, le=30)
    alertsDays: int | None = Field(default=None, ge=7, le=365)
    batteryDays: int | None = Field(default=None, ge=7, le=365)
    messages: RetentionSetting | None = None
    locations: RetentionSetting | None = None

    @model_validator(mode="after")
    def _legacy_bounds(self) -> PutSettingsRequest:
        for legacy, days, name in (
            (self.messages, self.messagesDays, "messages"),
            (self.locations, self.locationsDays, "locations"),
        ):
            if legacy is not None and days is None:
                lo, hi = (1, 365) if name == "messages" else (1, 30)
                if not lo <= legacy.days() <= hi:
                    raise ValueError(f"{name} must be {lo}..{hi} days")
        return self


@router.put("/settings", dependencies=[Depends(require_admin_write_rate_limit)])
def put_settings(req: PutSettingsRequest) -> RetentionSettings:
    return settings_store.set_retention(
        messagesDays=req.messagesDays,
        locationsDays=req.locationsDays,
        alertsDays=req.alertsDays,
        batteryDays=req.batteryDays,
        messages=req.messages,
        locations=req.locations,
    )


@router.get("/settings")
def get_settings() -> RetentionSettings:
    return settings_store.get_retention()


class CreateFamilyRequest(BaseModel):
    name: str


class PatchFamilyRequest(BaseModel):
    name: str | None = None


@router.get("/families")
def list_families() -> list[Family]:
    return families_store.list_families()


@router.post("/families", dependencies=[Depends(require_admin_write_rate_limit)])
def create_family(
    req: CreateFamilyRequest, authed: Annotated[AuthedUser, Depends(require_super)]
) -> Family:
    return families_store.create_family(name=req.name, created_by=authed.uid)


@router.patch("/families/{fid}", dependencies=[Depends(require_admin_write_rate_limit)])
def patch_family(fid: str, req: PatchFamilyRequest) -> Family:
    name = req.name
    if name is not None:
        try:
            name = families_store.validate_family_name(name)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    try:
        return families_store.update_family(fid, name=name)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


ImsiPath = Annotated[str, Path(pattern=r"^\d{14,15}$")]


def _soracom_client(settings: Settings) -> soracom.SoracomClient:
    if not settings.soracom_configured:
        raise HTTPException(status_code=503, detail="Soracom not configured")
    assert settings.soracom_auth_key_id and settings.soracom_auth_key
    return soracom.SoracomClient(settings.soracom_auth_key_id, settings.soracom_auth_key)


def _soracom_502(exc: soracom.SoracomError) -> HTTPException:
    return HTTPException(status_code=502, detail=f"soracom: {exc.detail}")


@router.get("/soracom/sims")
def list_soracom_sims(
    settings: Annotated[Settings, Depends(get_app_settings)],
) -> dict[str, object]:
    out: dict[str, object] = {
        "configured": settings.soracom_configured,
        "group": settings.soracom_beam_group,
        "groupId": None,
        "sims": [],
    }
    if not settings.soracom_configured:
        return out
    try:
        with _soracom_client(settings) as client:
            gid = soracom.find_group(client, settings.soracom_beam_group)
            sims = soracom.list_sims(client)
    except soracom.SoracomError as exc:
        raise _soracom_502(exc) from None
    out["groupId"] = gid
    out["sims"] = [
        {
            "imsi": s.imsi,
            "iccid": s.iccid,
            "status": s.status,
            "groupId": s.groupId,
            "enrolled": gid is not None and s.groupId == gid,
            "name": s.name,
            "subscription": s.subscription,
        }
        for s in sims
    ]
    return out


@router.post(
    "/soracom/sims/{imsi}/enroll", dependencies=[Depends(require_admin_write_rate_limit)]
)
def enroll_soracom_sim(
    imsi: ImsiPath,
    settings: Annotated[Settings, Depends(get_app_settings)],
    authed: Annotated[AuthedUser, Depends(require_super)],
) -> dict[str, object]:
    try:
        with _soracom_client(settings) as client:
            gid = soracom.ensure_group(
                client, settings.soracom_beam_group, settings.soracom_beam_destination
            )
            soracom.enroll(client, imsi, gid)
    except soracom.SoracomError as exc:
        raise _soracom_502(exc) from None
    log.info("soracom enroll imsi=%s group=%s by=%s", soracom.mask_imsi(imsi), gid, authed.uid)
    return {"ok": True, "imsi": imsi, "groupId": gid}


@router.post("/soracom/enroll-all", dependencies=[Depends(require_admin_write_rate_limit)])
def enroll_all_soracom_sims(
    settings: Annotated[Settings, Depends(get_app_settings)],
    authed: Annotated[AuthedUser, Depends(require_super)],
) -> dict[str, object]:
    enrolled: list[str] = []
    already: list[str] = []
    try:
        with _soracom_client(settings) as client:
            gid = soracom.find_group(client, settings.soracom_beam_group)
            todo: list[str] = []
            for s in soracom.list_sims(client):
                if not s.imsi:
                    continue
                if gid is not None and s.groupId == gid:
                    already.append(s.imsi)
                else:
                    todo.append(s.imsi)
            if todo:
                gid = soracom.ensure_group(
                    client, settings.soracom_beam_group, settings.soracom_beam_destination
                )
            for imsi in todo:
                soracom.enroll(client, imsi, str(gid))
                enrolled.append(imsi)
                log.info(
                    "soracom enroll imsi=%s group=%s by=%s", soracom.mask_imsi(imsi), gid, authed.uid
                )
    except soracom.SoracomError as exc:
        raise _soracom_502(exc) from None
    return {"ok": True, "enrolled": enrolled, "already": already}
