"""`/api/admin/*`: super-only user, allow-list and device administration.

Write routes carry a per-uid rate limit (`RATE_LIMIT_ADMIN_WRITE_LIMIT` per window, default 60/min).
"""

from __future__ import annotations

import hashlib
import os
import secrets
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

from firebase_admin import auth as fb_auth

from app.auth import AuthedUser, principal_for, require_super, set_claims
from app.broker import BrokerClient
from app.db.firestore import get_db
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

router = APIRouter(prefix="/api/admin", dependencies=[Depends(require_super)])

MQTT_PASSWORD_BYTES = 24

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


    """`app.state.emqx_admin` if set (tests inject one), else a fresh `make_broker_admin`."""
    existing = getattr(request.app.state, "emqx_admin", None)
    if existing is not None:
        return existing


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


    try:
        user = users_store.create_user(
            alias=req.alias,
            display_name=req.displayName,
            email=req.email,
            phone=phone,
            role=req.role,
            family_id=family_id,
        )
    except (users_store.AliasTaken, users_store.InvalidAlias) as exc:
        # roll back the Auth account so a rejected alias leaves no orphaned Auth user
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
    brokerPush: Literal["pushed", "manual"]
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


def _broker_push_outcome(
    device_id: str, result: EmqxResult
) -> tuple[Literal["pushed", "manual"], list[str] | None]:
    if result == "ok":
        return "pushed", None
    return "manual", _manual_acl_lines(device_id)


def _create_device_impl(
    req: CreateDeviceRequest,
    broker: BrokerClient,
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

    # generate the MQTT password and HMAC key; the plaintext exists only here
    password = secrets.token_urlsafe(MQTT_PASSWORD_BYTES)
    password_hash = hashlib.sha256(password.encode("utf-8")).hexdigest()
    hmac_key = secrets.token_bytes(HMAC_KEY_BYTES)

    devices_store.create_device(
        owner_uid=owner_uid,
        label=req.label,
        # a device's family is copied from its owner
        family_id=owner.familyId if owner is not None else None,
        mqtt_password_hash=password_hash,
        default_to_uid=default_to_uid,
        apn=apn,
    )
    # the pager is a delivery backend of its owner; without this it is undeliverable
    backends_store.create_backend(
    )
    # pick up incoming `locate` edges that predate this device
    allow_store.recompute_locatable_by_for_owner(owner_uid)

    # PROTOCOL.md §3.7: device creation bumps and pushes the new device's book. Best-effort.
    # seed `cfg.sms` (implied contacts for an open owner)
    book.rederive_sms_contacts(owner_uid, broker)

    # push the broker credential and ACL before handing out a setup code
    host, ca = _bootstrap_host_and_ca(settings)
    now = datetime.now(UTC)
    try:
        setup_code = devsetup.issue(
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
        )
    except ca_resolve.PublicBaseUrlRequired as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    expires_at = now + timedelta(minutes=devsetup.EXPIRY_MINUTES)

    assert device is not None
    return DeviceSetupCodeResponse(
        device=device,
        setupCode=setup_code,
        expiresAt=expires_at,
        brokerPush=broker_push,
        manualAcl=manual_acl,
    )


@router.post("/devices", dependencies=[Depends(require_admin_write_rate_limit)])
def create_device(
    req: CreateDeviceRequest,
    broker: Annotated[BrokerClient, Depends(get_broker)],
    settings: Annotated[Settings, Depends(get_app_settings)],
) -> DeviceSetupCodeResponse:
    return _create_device_impl(req, broker, emqx, settings)


@router.get("/devices")
def list_devices(family: Annotated[str | None, Query()] = None) -> list[Device]:
    """`?family=` narrows the listing to one family."""
    devices = devices_store.list_devices()
    if family is not None:
        devices = [d for d in devices if d.familyId == family]
    return devices


        raise HTTPException(status_code=404, detail="no such device")
    return {"ok": True}


@router.delete("/devices/{device_id}", dependencies=[Depends(require_admin_write_rate_limit)])


def _rotate_credentials_impl(
    device_id: str,
    broker: BrokerClient,
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
    device_secrets_store.rotate(device_id, hmac_key, password_hash)
    # the alarm and its window concern the old key
    devices_store.clear_auth_alarm(device_id)
    # drop the cached secret so envelopes are not verified against the deleted `hmacKey`
    ingest.invalidate_secret_cache(device_id)
    devices_store.set_provision_state(device_id, "issued")

    emqx_result = emqx.ensure_device(device.mqttUsername, password, device_id)
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
        )
    except ca_resolve.PublicBaseUrlRequired as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    expires_at = now + timedelta(minutes=devsetup.EXPIRY_MINUTES)

    updated = devices_store.get_device(device_id)
    assert updated is not None
    broker_push, manual_acl = _broker_push_outcome(device_id, emqx_result)
    return DeviceSetupCodeResponse(
        device=updated,
        setupCode=setup_code,
        expiresAt=expires_at,
        brokerPush=broker_push,
        manualAcl=manual_acl,
    )


@router.post(
    "/devices/{device_id}/rotate-credentials",
    dependencies=[Depends(require_admin_write_rate_limit)],
)
def rotate_credentials(
    device_id: str,
    broker: Annotated[BrokerClient, Depends(get_broker)],
    settings: Annotated[Settings, Depends(get_app_settings)],
    ingest: Annotated[Ingest, Depends(get_ingest)],
) -> DeviceSetupCodeResponse:
    return _rotate_credentials_impl(device_id, broker, emqx, settings, ingest)


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


@router.get("/apn-presets")
def list_apn_presets() -> list[apn_presets.ApnPreset]:
    """Carrier APN choices for the device forms."""


    if devices_store.get_device(device_id) is None:
        raise HTTPException(status_code=404, detail="no such device")
    try:
        apn = apn_presets.validate_apn(req.apn)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    devices_store.set_apn(device_id, apn)
    return {"apn": apn}


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


def _load_firmware_index(settings: Settings) -> firmware.FirmwareIndex:
    try:
        return firmware.load_index(settings)
    except firmware.FirmwareNotConfigured:
        raise HTTPException(status_code=503, detail="OTA not configured") from None
    except firmware.FirmwareIndexUnavailable:
        raise HTTPException(status_code=502, detail="firmware index unavailable") from None


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
    return {"builds": builds}


    device_id: str,
    req: OtaRequest,
) -> dict[str, object]:
    if (req.target is None) == (not req.cancel):
        raise HTTPException(status_code=422, detail="send exactly one of target or cancel")
    dev = devices_store.get_device(device_id)
    if dev is None:
        raise HTTPException(status_code=404, detail="no such device")
    if not (settings.fw_index_url and settings.fw_bucket_base):
        raise HTTPException(status_code=503, detail="OTA not configured")
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
    if dev.status.img and dev.status.img.strip().lower()[:16] == target_build.id16:
        raise HTTPException(status_code=409, detail="already running that build")
    choice = firmware.choose(index, req.target, dev.status.img)
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
        },
    )


class PutSettingsRequest(BaseModel):


@router.put("/settings", dependencies=[Depends(require_admin_write_rate_limit)])
def put_settings(req: PutSettingsRequest) -> RetentionSettings:


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
