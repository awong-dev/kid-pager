"""`/api/family/*`: family admin routes (role `admin` in their own family, or `super` naming one).

Member, group and device writes reuse the `app/routers/admin.py` implementations.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Annotated, Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request
from firebase_admin import auth as fb_auth
from google.api_core.exceptions import AlreadyExists
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app import (
    apn_presets,
    authn_home,
    book,
    chat_subscribe,
    devcfg,
    inbound_text,
    sms_text,
)
from app import policy as policy_module
from app.auth import Principal, require_family_admin, set_claims
from app.book import rederive_family_sms_contacts, rederive_sms_contacts
from app.broker import BrokerClient
from app.broker_admin import BrokerAdmin, make_broker_admin
from app.config import Settings, auth_backend
from app.db.firestore import get_db
from app.familyexport import normalize_email
from app.ingest import Ingest
from app.routers import admin as admin_router
from app.routers import conversations as conversations_router
from app.routing import Routing
from app.store import alerts as alerts_store
from app.store import allow as allow_store
from app.store import contacts as contacts_store
from app.store import conversations as conversations_store
from app.store import devices as devices_store
from app.store import externals as externals_store
from app.store import families as families_store
from app.store import fed_state
from app.store import held_sms as held_sms_store
from app.store import hooks as hooks_store
from app.store import rate_limits as rate_limits_store
from app.store import users as users_store
from app.store.alerts import Alert
from app.store.devices import Device
from app.store.families import Family
from app.store.users import InvalidAlias, Policy, User

logger = logging.getLogger("relay.family")

router = APIRouter(prefix="/api/family")

# The family in scope: an admin's own, or the one a `super` names via `?family=` / `X-Family`.
FamilyScope = Annotated[tuple[Principal, str], Depends(require_family_admin)]


def require_family_write_rate_limit(scope: FamilyScope) -> None:
    """Per-uid write limiter shared with the admin surface (one person's overall rate)."""
    principal, _ = scope
    limit, window_s = admin_router._admin_write_rate_limit()
    if not rate_limits_store.check_and_increment(
        f"admin:{principal.uid}", limit=limit, window_s=window_s
    ):
        raise HTTPException(status_code=429, detail="admin rate limit exceeded; try again later")


def get_broker(request: Request) -> BrokerClient:
    return request.app.state.broker


def get_ingest(request: Request) -> Ingest:
    return request.app.state.ingest


def get_emqx(request: Request) -> BrokerAdmin:
    """Fallback-to-a-fresh-instance, like the admin router's `get_emqx`."""
    existing = getattr(request.app.state, "emqx_admin", None)
    if existing is not None:
        return existing
    return make_broker_admin(request.app.state.settings, request.app.state.broker)


def get_app_settings(request: Request) -> Settings:
    return request.app.state.settings


class HookDisclosure(BaseModel):
    """A hook that reads this family's messages."""

    id: str
    name: str
    mode: str
    host: str


class FamilyOut(Family):
    memberCount: int
    deviceCount: int
    hooks: list[HookDisclosure] = Field(default_factory=list)


def _family_out(family_id: str) -> FamilyOut:
    family = families_store.get_family(family_id)
    if family is None:
        raise HTTPException(status_code=404, detail="no such family")
    member_count = sum(1 for u in users_store.list_users() if u.familyId == family_id)
    device_count = sum(1 for d in devices_store.list_devices() if d.familyId == family_id)
    return FamilyOut(
        # `federation` is the effective state (server and family switch), not the stored flag
        **{**family.model_dump(), "federation": fed_state.family_enabled(family_id)},
        memberCount=member_count,
        deviceCount=device_count,
        hooks=_hook_disclosures(family_id),
    )


def _hook_disclosures(family_id: str) -> list[HookDisclosure]:
    return [
        HookDisclosure(id=h.id, name=h.name, mode=h.mode, host=urlsplit(h.url).netloc)
        for h in hooks_store.list_hooks()
        if h.enabled and h.covers({family_id})
    ]


@router.get("")
def get_family(scope: FamilyScope) -> FamilyOut:
    _, family_id = scope
    return _family_out(family_id)


class PatchFamilyRequest(BaseModel):
    name: str | None = None
    # notice, link-check and top-up policy
    settings: families_store.FamilySettingsPatch | None = None

    @model_validator(mode="after")
    def _something_to_patch(self) -> PatchFamilyRequest:
        if self.name is None and self.settings is None:
            raise ValueError("send a name and/or settings")
        return self


@router.patch("", dependencies=[Depends(require_family_write_rate_limit)])
def patch_family(req: PatchFamilyRequest, scope: FamilyScope) -> FamilyOut:
    """Rename the family (web only: no book bump, no push) and/or patch `settings` partially."""
    _, family_id = scope
    name = None
    if req.name is not None:
        try:
            name = families_store.validate_family_name(req.name)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    try:
        families_store.update_family(family_id, name=name, settings=req.settings)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="no such family") from exc
    return _family_out(family_id)


class MembersOut(BaseModel):
    members: list[User]


@router.get("/members")
def list_members(scope: FamilyScope) -> MembersOut:
    _, family_id = scope
    return MembersOut(members=[u for u in users_store.list_users() if u.familyId == family_id])


class CreateMemberRequest(BaseModel):
    alias: str
    displayName: str
    email: str | None = None
    phone: str | None = None
    role: Literal["admin", "member"] = "member"


@router.post("/members", dependencies=[Depends(require_family_write_rate_limit)])
def create_member(
    req: CreateMemberRequest,
    scope: FamilyScope,
    broker: Annotated[BrokerClient, Depends(get_broker)],
) -> User:
    """Same flow as the admin `create_user`, with the scope family fixed."""
    _, family_id = scope
    home = auth_backend() == "home"
    # at home the alias is the login name; email/phone are optional
    if not home and not req.email and not req.phone:
        raise HTTPException(status_code=400, detail="email or phone is required")

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
        uid = authn_home.new_uid()
    else:
        kwargs: dict[str, object] = {}
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
        if not home:
            fb_auth.delete_user(uid)
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    set_claims(user.uid, user.role, user.familyId)
    # the book lists the whole family, so every member's book gains the newcomer
    book.bump_and_push(book.family_uids(family_id), broker, reason="member_create")
    return user


class PatchMemberRequest(BaseModel):
    displayName: str | None = None
    role: Literal["admin", "member"] | None = None
    disabled: bool | None = None
    # the member's two policy pickers, validated against `app.policy.OUT`/`IN`
    policy: Policy | None = None


@router.patch("/members/{uid}", dependencies=[Depends(require_family_write_rate_limit)])
def patch_member(
    uid: str,
    req: PatchMemberRequest,
    scope: FamilyScope,
    broker: Annotated[BrokerClient, Depends(get_broker)],
) -> User:
    _, family_id = scope
    existing = users_store.get_user(uid)
    if existing is None or existing.familyId != family_id:
        raise HTTPException(status_code=404, detail="no such user in this family")

    if req.policy is not None:
        if req.policy.out not in policy_module.OUT:
            raise HTTPException(status_code=400, detail=f"invalid policy.out: {req.policy.out!r}")
        if req.policy.in_ not in policy_module.IN:
            raise HTTPException(status_code=400, detail=f"invalid policy.in: {req.policy.in_!r}")
        # No store setter for `policy`: written directly, before the user is re-read.
        get_db().collection("users").document(uid).update(
            {"policy": {"out": req.policy.out, "in": req.policy.in_}}
        )

    policy_changed = req.policy is not None and req.policy != existing.policy
    result = admin_router._patch_user_impl(
        uid,
        display_name=req.displayName,
        email=None,
        phone=None,
        role=req.role,
        family_id=None,
        disabled=req.disabled,
        broker=broker,
    )
    if policy_changed:
        # a policy change flips `sendable` in this member's book and every sibling's
        book.bump_and_push(book.family_uids(family_id) | {uid}, broker, reason="policy")
        # an open/any_sms outbound policy implies every family contact on `cfg.sms`
        rederive_sms_contacts(uid, broker)
    return result


class InviteResponse(BaseModel):
    code: str
    expiresAt: datetime


@router.post("/people/{uid}/invite", dependencies=[Depends(require_family_write_rate_limit)])
def invite_member(uid: str, scope: FamilyScope) -> InviteResponse:
    """One-time, seven-day code for the member to set a password or passkey (home only).

    A family admin cannot mint one for a `super`: redeeming it signs in as that account.
    """
    principal, family_id = scope
    if auth_backend() != "home":
        raise HTTPException(status_code=404, detail="not found")
    user = users_store.get_user(uid)
    if user is None or user.familyId != family_id or user.kind != "person":
        raise HTTPException(status_code=404, detail="no such person in this family")
    if user.role == "super" and principal.role != "super":
        raise HTTPException(status_code=403, detail="only a super admin can invite a super admin")
    code, expires = authn_home.mint_invite(uid, principal.uid)
    return InviteResponse(code=code, expiresAt=expires)


class CreateAccountRequest(BaseModel):
    email: str


@router.post("/people/{uid}/account", dependencies=[Depends(require_family_write_rate_limit)])
def create_person_account(uid: str, req: CreateAccountRequest, scope: FamilyScope) -> User:
    """Give a person with no sign-in a Firebase account (cloud only); 409 if one exists.

    A family admin cannot do this for a `super`.
    """
    principal, family_id = scope
    if auth_backend() == "home":
        raise HTTPException(status_code=404, detail="not found")
    user = users_store.get_user(uid)
    if user is None or user.familyId != family_id or user.kind != "person":
        raise HTTPException(status_code=404, detail="no such person in this family")
    if user.role == "super" and principal.role != "super":
        raise HTTPException(status_code=403, detail="only a super admin can do this for a super admin")
    email = normalize_email(req.email)
    if email is None:
        raise HTTPException(status_code=400, detail="not an email address")
    if user.email:
        raise HTTPException(status_code=409, detail="this person already has an account")
    try:
        fb_auth.create_user(uid=uid, email=email)
    except fb_auth.UidAlreadyExistsError as exc:
        raise HTTPException(status_code=409, detail="this person already has an account") from exc
    except fb_auth.EmailAlreadyExistsError as exc:
        raise HTTPException(status_code=409, detail="email already registered") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    set_claims(uid, user.role, user.familyId)
    return users_store.update_user(uid, email=email)


def _require_family_owner(owner_alias: str, family_id: str) -> str:
    owner_uid = users_store.get_uid_for_alias(owner_alias)
    if owner_uid is None:
        raise HTTPException(status_code=400, detail=f"unknown alias: {owner_alias!r}")
    owner = users_store.get_user(owner_uid)
    if owner is None or owner.familyId != family_id:
        raise HTTPException(status_code=403, detail="owner is not a member of this family")
    return owner_uid


def _require_family_device(device_id: str, family_id: str) -> Device:
    device = devices_store.get_device(device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="no such device")
    if device.familyId != family_id:
        raise HTTPException(status_code=403, detail="device belongs to another family")
    return device


@router.get("/devices")
def list_devices(scope: FamilyScope) -> list[Device]:
    _, family_id = scope
    return [d for d in devices_store.list_devices() if d.familyId == family_id]


@router.post("/devices", dependencies=[Depends(require_family_write_rate_limit)])
def create_device(
    req: admin_router.CreateDeviceRequest,
    scope: FamilyScope,
    broker: Annotated[BrokerClient, Depends(get_broker)],
    emqx: Annotated[BrokerAdmin, Depends(get_emqx)],
    settings: Annotated[Settings, Depends(get_app_settings)],
) -> admin_router.DeviceSetupCodeResponse:
    _, family_id = scope
    _require_family_owner(req.ownerAlias, family_id)
    return admin_router._create_device_impl(req, broker, emqx, settings)


@router.patch("/devices/{device_id}", dependencies=[Depends(require_family_write_rate_limit)])
def patch_device(
    device_id: str, req: admin_router.PatchDeviceRequest, scope: FamilyScope
) -> Device:
    """Edit the free-text `label`; the pager shows it after the next `rotate-credentials`."""
    _, family_id = scope
    _require_family_device(device_id, family_id)
    return admin_router._patch_device_impl(device_id, req)


@router.post(
    "/devices/{device_id}/rotate-credentials",
    dependencies=[Depends(require_family_write_rate_limit)],
)
def rotate_credentials(
    device_id: str,
    scope: FamilyScope,
    broker: Annotated[BrokerClient, Depends(get_broker)],
    emqx: Annotated[BrokerAdmin, Depends(get_emqx)],
    settings: Annotated[Settings, Depends(get_app_settings)],
    ingest: Annotated[Ingest, Depends(get_ingest)],
) -> admin_router.DeviceSetupCodeResponse:
    _, family_id = scope
    _require_family_device(device_id, family_id)
    return admin_router._rotate_credentials_impl(device_id, broker, emqx, settings, ingest)


@router.post(
    "/devices/{device_id}/revoke", dependencies=[Depends(require_family_write_rate_limit)]
)
def revoke_device(
    device_id: str,
    scope: FamilyScope,
    emqx: Annotated[BrokerAdmin, Depends(get_emqx)],
) -> Device:
    _, family_id = scope
    _require_family_device(device_id, family_id)
    return admin_router._revoke_device_impl(device_id, emqx)


@router.post("/devices/{device_id}/cfg", dependencies=[Depends(require_family_write_rate_limit)])
def push_cfg(
    device_id: str,
    req: admin_router.PushCfgRequest,
    scope: FamilyScope,
    broker: Annotated[BrokerClient, Depends(get_broker)],
) -> dict[str, bool]:
    _, family_id = scope
    _require_family_device(device_id, family_id)
    return admin_router._push_cfg_impl(device_id, req, broker)


@router.post("/devices/{device_id}/ca", dependencies=[Depends(require_family_write_rate_limit)])
def push_ca(
    device_id: str,
    req: admin_router.PushCaRequest,
    scope: FamilyScope,
    broker: Annotated[BrokerClient, Depends(get_broker)],
    settings: Annotated[Settings, Depends(get_app_settings)],
) -> dict[str, bool]:
    _, family_id = scope
    _require_family_device(device_id, family_id)
    return admin_router._push_ca_impl(device_id, req, broker, settings)


@router.get("/firmware")
def list_firmware(
    scope: FamilyScope,
    settings: Annotated[Settings, Depends(get_app_settings)],
    device: str,
) -> dict[str, list[dict[str, object]]]:
    """Published builds as seen by one in-family device."""
    _, family_id = scope
    _require_family_device(device, family_id)
    return admin_router._list_firmware_impl(settings, device)


@router.post("/devices/{device_id}/ota", dependencies=[Depends(require_family_write_rate_limit)])
def push_ota(
    device_id: str,
    req: admin_router.OtaRequest,
    scope: FamilyScope,
    broker: Annotated[BrokerClient, Depends(get_broker)],
    settings: Annotated[Settings, Depends(get_app_settings)],
) -> dict[str, object]:
    """Push or cancel an OTA for an in-family device."""
    principal, family_id = scope
    _require_family_device(device_id, family_id)
    return admin_router._push_ota_impl(device_id, req, principal.uid, broker, settings)


@router.get("/assets")
def list_assets(
    scope: FamilyScope,
    settings: Annotated[Settings, Depends(get_app_settings)],
    device: str,
) -> dict[str, object]:
    """Published font images as seen by one in-family device."""
    _, family_id = scope
    _require_family_device(device, family_id)
    return admin_router._list_assets_impl(settings, device)


@router.post("/devices/{device_id}/assets", dependencies=[Depends(require_family_write_rate_limit)])
def push_assets(
    device_id: str,
    req: admin_router.AssetsRequest,
    scope: FamilyScope,
    broker: Annotated[BrokerClient, Depends(get_broker)],
    settings: Annotated[Settings, Depends(get_app_settings)],
) -> dict[str, object]:
    """Push or cancel a font OTA for an in-family device."""
    principal, family_id = scope
    _require_family_device(device_id, family_id)
    return admin_router._push_assets_impl(device_id, req, principal.uid, broker, settings)


@router.get("/apn-presets")
def list_apn_presets(scope: FamilyScope) -> list[apn_presets.ApnPreset]:
    """Carrier APN choices for the device forms."""
    return admin_router._list_apn_presets_impl()


@router.put("/devices/{device_id}/apn", dependencies=[Depends(require_family_write_rate_limit)])
def set_device_apn(
    device_id: str, req: admin_router.SetApnRequest, scope: FamilyScope
) -> dict[str, str | None]:
    """Set the stored APN of an in-family device."""
    _, family_id = scope
    _require_family_device(device_id, family_id)
    return admin_router._set_device_apn_impl(device_id, req)


@router.delete("/devices/{device_id}", dependencies=[Depends(require_family_write_rate_limit)])
def delete_device(
    device_id: str,
    scope: FamilyScope,
    emqx: Annotated[BrokerAdmin, Depends(get_emqx)],
) -> dict[str, bool]:
    _, family_id = scope
    _require_family_device(device_id, family_id)
    return admin_router._delete_device_impl(device_id, emqx)


class CreateGroupRequest(BaseModel):
    name: str
    alias: str
    memberUids: list[str] = Field(min_length=2)


class CreateGroupResponse(BaseModel):
    convKey: str
    alias: str


@router.post("/groups", status_code=201, dependencies=[Depends(require_family_write_rate_limit)])
def create_group(
    req: CreateGroupRequest,
    scope: FamilyScope,
    broker: Annotated[BrokerClient, Depends(get_broker)],
) -> CreateGroupResponse:
    principal, family_id = scope
    member_uids = sorted(set(req.memberUids))
    for uid in member_uids:
        user = users_store.get_user(uid)
        if user is None:
            raise HTTPException(status_code=404, detail=f"no such user: {uid!r}")
        if user.kind == "external":
            raise HTTPException(status_code=400, detail="an SMS contact cannot join a group")
        if uid == principal.uid:
            continue
        in_family = user.familyId == family_id
        has_edge = allow_store.is_message_allowed(
            principal.uid, uid
        ) or allow_store.is_message_allowed(uid, principal.uid)
        if not in_family and not has_edge:
            raise HTTPException(
                status_code=403,
                detail=(
                    f"{uid!r} is not in this family and has no message edge "
                    "with the creator"
                ),
            )

    try:
        conv = conversations_store.create_group(
            name=req.name, alias=req.alias, member_uids=member_uids, created_by=principal.uid
        )
    except InvalidAlias as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except AlreadyExists as exc:
        raise HTTPException(status_code=409, detail="alias already taken") from exc

    # message-only mutual edges, never `locate`, so a member spanning two families can join
    conversations_router._create_missing_message_edges(conv.uids)
    conversations_router._push_book_to_members(conv.uids, broker)
    return CreateGroupResponse(convKey=conv.convKey, alias=req.alias)


class ApprovedPerson(BaseModel):
    alias: str
    message: bool = True
    locate: bool = False


class ApprovedContact(BaseModel):
    uid: str
    message: bool = True


class PutApprovedRequest(BaseModel):
    # a stale client still sending `numbers` gets a 422
    model_config = ConfigDict(extra="forbid")

    people: list[ApprovedPerson] = []
    contacts: list[ApprovedContact] = []


class ApprovedOut(BaseModel):
    people: list[ApprovedPerson]
    contacts: list[ApprovedContact]


def _require_family_member(uid: str, family_id: str) -> User:
    member = users_store.get_user(uid)
    if member is None or member.familyId != family_id:
        raise HTTPException(status_code=404, detail="no such user in this family")
    return member


def _approved_out(uid: str, family_id: str) -> ApprovedOut:
    """The member's outgoing edges as the web lists them (shared by GET and PUT)."""
    people: list[ApprovedPerson] = []
    contacts: list[ApprovedContact] = []
    for edge in allow_store.list_edges():
        if edge.fromUid != uid:
            continue
        peer = users_store.get_user(edge.toUid)
        if peer is None:
            continue
        if peer.kind == "external":
            # a Google Chat subscription edge is managed under Google Chat
            if peer.ownerFamilyId == family_id and not peer.chat:
                contacts.append(ApprovedContact(uid=peer.uid, message=edge.message))
        else:
            people.append(
                ApprovedPerson(alias=peer.alias, message=edge.message, locate=edge.locate)
            )
    people.sort(key=lambda p: p.alias)
    contacts.sort(key=lambda c: c.uid)
    return ApprovedOut(people=people, contacts=contacts)


@router.get("/members/{uid}/approved")
def get_approved(uid: str, scope: FamilyScope) -> ApprovedOut:
    _, family_id = scope
    _require_family_member(uid, family_id)
    return _approved_out(uid, family_id)


@router.put("/members/{uid}/approved", dependencies=[Depends(require_family_write_rate_limit)])
def put_approved(
    uid: str,
    req: PutApprovedRequest,
    scope: FamilyScope,
    broker: Annotated[BrokerClient, Depends(get_broker)],
) -> ApprovedOut:
    _, family_id = scope
    _require_family_member(uid, family_id)

    # People: resolve and check before writing, so a bad alias or cross-family `locate` 400s first.
    resolved_people: list[tuple[str, ApprovedPerson]] = []
    for p in req.people:
        peer_uid = users_store.get_uid_for_alias(p.alias)
        if peer_uid is None:
            raise HTTPException(status_code=400, detail=f"unknown alias: {p.alias!r}")
        if peer_uid == uid:
            raise HTTPException(status_code=400, detail="cannot approve yourself")
        try:
            allow_store.check_locate_family(uid, peer_uid, p.locate)
        except allow_store.LocateCrossFamily as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        resolved_people.append((peer_uid, p))

    # Contacts: every uid must be one of this family's SMS contacts; always message-only.
    for c in req.contacts:
        contact = users_store.get_user(c.uid)
        if (
            contact is None
            or contact.kind != "external"
            or contact.ownerFamilyId != family_id
            or contact.chat
        ):
            raise HTTPException(status_code=404, detail=f"no such contact: {c.uid}")

    for peer_uid, p in resolved_people:
        allow_store.set_edge(uid, peer_uid, message=p.message, locate=p.locate)
    people_changed = bool(resolved_people)

    # Full replacement: any outgoing edge of `uid` not listed is deleted, person or external.
    kept_people_uids = {peer_uid for peer_uid, _ in resolved_people}
    kept_contact_uids = {c.uid for c in req.contacts}
    peers: dict[str, User | None] = {}
    for edge in allow_store.list_edges():
        if edge.fromUid != uid:
            continue
        if edge.toUid not in peers:
            peers[edge.toUid] = users_store.get_user(edge.toUid)
        peer = peers[edge.toUid]
        if peer is None:
            continue
        if peer.kind == "external":
            if peer.chat:
                continue  # the subscribe edge belongs to Google Chat, not this editor
            if edge.toUid not in kept_contact_uids:
                allow_store.delete_edge(uid, edge.toUid)
        elif edge.toUid not in kept_people_uids:
            allow_store.delete_edge(uid, edge.toUid)
            people_changed = True

    for c in req.contacts:
        allow_store.set_edge(uid, c.uid, message=c.message, locate=False)

    rederive_family_sms_contacts(family_id, broker)
    if people_changed:
        # `c[]` projects the member's people edges; bump then push for every device `uid` owns
        for device in devices_store.list_devices(owner_uid=uid):
            contacts_store.bump_book_version(device.id)
            devcfg.push_book(device.id, broker)

    return _approved_out(uid, family_id)


class ContactFedOut(BaseModel):
    linkId: str | None = None  # null once the link is gone
    name: str | None = None  # the other family's name
    host: str | None = None  # and its server
    rid: str | None = None


class ContactOut(BaseModel):
    uid: str
    alias: str
    phone: str | None
    displayName: str
    approvedFor: list[str]
    # A person in a linked family; `sendable`/`reason` are the book's link-state verdict.
    fed: ContactFedOut | None = None
    sendable: bool | None = None
    reason: str | None = None
    # Members who hold the contact through their outbound policy alone (open / any_sms).
    impliedFor: list[str] = []


def _name_taken(family_id: str, exc: externals_store.ContactNameTaken) -> HTTPException:
    logger.info("contact name taken family=%s", family_id)
    return HTTPException(status_code=409, detail=str(exc))


def _fed_fields(ext: users_store.User) -> dict[str, object]:
    if not (ext.fed or {}).get("remoteFedId"):
        return {}
    link = book.fed_link_for(ext)
    reason = book.fed_link_reason(ext)
    rid = (ext.fed or {}).get("rid")
    return {
        "fed": ContactFedOut(
            linkId=link.id if link else None,
            name=link.remote.name if link else None,
            host=link.remote.host if link else None,
            rid=str(rid) if rid else None,
        ),
        "sendable": reason is None,
        "reason": reason,
    }


def _list_contacts(family_id: str) -> list[ContactOut]:
    members = [u for u in users_store.list_users() if u.familyId == family_id]
    member_uids = {u.uid for u in members}
    approved_for: dict[str, set[str]] = {}
    has_edge: dict[str, set[str]] = {}
    for edge in allow_store.list_edges():
        if edge.fromUid in member_uids:
            has_edge.setdefault(edge.toUid, set()).add(edge.fromUid)
            if edge.message:
                approved_for.setdefault(edge.toUid, set()).add(edge.fromUid)
    implying = [
        u.uid
        for u in members
        if u.kind == "person"
        and not u.disabled
        and policy_module.rule(u.policy.out, "external") == "any"
    ]

    out = [
        ContactOut(
            uid=ext.uid,
            alias=ext.alias,
            phone=ext.phone,
            displayName=ext.displayName,
            approvedFor=sorted(approved_for.get(ext.uid, set())),
            impliedFor=sorted(set(implying) - has_edge.get(ext.uid, set())),
            **_fed_fields(ext),
        )
        for ext in externals_store.list_family_contacts(family_id)
        if not ext.chat
    ]
    out.sort(key=lambda c: c.displayName.casefold())
    return out


@router.get("/contacts")
def list_contacts(scope: FamilyScope) -> list[ContactOut]:
    _, family_id = scope
    return _list_contacts(family_id)


class CreateContactRequest(BaseModel):
    phone: str
    name: str = Field(min_length=1)


def _contact_out(family_id: str, external: User) -> ContactOut:
    """The contact as `GET /contacts` reports it (true approvedFor/impliedFor)."""
    return next(c for c in _list_contacts(family_id) if c.uid == external.uid)


@router.post(
    "/contacts", status_code=201, dependencies=[Depends(require_family_write_rate_limit)]
)
def create_contact(
    req: CreateContactRequest,
    scope: FamilyScope,
    broker: Annotated[BrokerClient, Depends(get_broker)],
) -> ContactOut:
    """Create an external with no edges; 409 if another family contact has the name."""
    _, family_id = scope
    try:
        external = externals_store.get_or_create(family_id, req.phone, req.name)
    except externals_store.ContactNameTaken as exc:
        raise _name_taken(family_id, exc) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    rederive_family_sms_contacts(family_id, broker)
    return _contact_out(family_id, external)


class PatchContactRequest(BaseModel):
    name: str = Field(min_length=1)


def _require_family_contact(uid: str, family_id: str) -> User:
    external = users_store.get_user(uid)
    # a contact belongs to one family
    if external is None or external.kind != "external" or external.ownerFamilyId != family_id:
        raise HTTPException(status_code=404, detail="no such contact")
    return external


@router.patch("/contacts/{uid}", dependencies=[Depends(require_family_write_rate_limit)])
def patch_contact(
    uid: str,
    req: PatchContactRequest,
    scope: FamilyScope,
    broker: Annotated[BrokerClient, Depends(get_broker)],
) -> ContactOut:
    _, family_id = scope
    external = _require_family_contact(uid, family_id)
    try:
        updated = externals_store.rename(family_id, external.uid, req.name)
    except externals_store.ContactNameTaken as exc:
        raise _name_taken(family_id, exc) from exc
    rederive_family_sms_contacts(family_id, broker)
    return _contact_out(family_id, updated)


@router.delete("/contacts/{uid}", dependencies=[Depends(require_family_write_rate_limit)])
def delete_contact(
    uid: str,
    scope: FamilyScope,
    broker: Annotated[BrokerClient, Depends(get_broker)],
) -> dict[str, bool]:
    """Delete the contact: its `allow` edges, name reservation, user and alias. Messages stay."""
    _, family_id = scope
    _require_family_contact(uid, family_id)
    externals_store.delete(family_id, uid)
    rederive_family_sms_contacts(family_id, broker)
    return {"ok": True}


class AlertsOut(BaseModel):
    alerts: list[Alert]


@router.get("/alerts")
def list_alerts(scope: FamilyScope, status: Literal["open", "all"] = "open") -> AlertsOut:
    _, family_id = scope
    return AlertsOut(alerts=alerts_store.list_alerts(family_id, status))


def _require_open_family_alert(alert_id: str, family_id: str) -> Alert:
    alert = alerts_store.get(family_id, alert_id)
    if alert is None:
        raise HTTPException(status_code=404, detail="no such alert")
    if alert.status != "open":
        raise HTTPException(status_code=409, detail="alert already decided")
    return alert


class ApproveAlertRequest(BaseModel):
    """Body for all three alert kinds; dispatch is on the stored alert's `kind`."""

    name: str | None = None
    forAlias: str | None = None
    # `contact_request` approval ignores these; `mode:"create"` is refused.
    mode: Literal["link", "create"] | None = None
    alias: str | None = None


class AlertDecision(BaseModel):
    """The decided `sms_unknown` alert plus how many held texts were delivered / left held."""

    alert: Alert
    delivered: int = 0
    undelivered: int = 0


def _approve_sms_unknown(
    alert: Alert,
    req: ApproveAlertRequest,
    family_id: str,
    broker: BrokerClient,
    routing: Routing,
) -> tuple[int, int]:
    """Steps (a)-(e) in order, each idempotent so a retry finishes; returns `(delivered, undelivered)`."""
    if not req.name:
        raise HTTPException(status_code=400, detail="name is required")
    if alert.peerPhone is None:
        raise HTTPException(status_code=400, detail="alert has no phone number")

    target_uid = alert.subjectUid
    if target_uid is None:
        if not req.forAlias:
            raise HTTPException(status_code=400, detail="forAlias is required for this alert")
        target_uid = users_store.get_uid_for_alias(req.forAlias)
        if target_uid is None:
            raise HTTPException(status_code=400, detail=f"unknown alias: {req.forAlias!r}")
    target = users_store.get_user(target_uid)
    if target is None or target.familyId != family_id:
        raise HTTPException(status_code=403, detail="target is not a member of this family")

    # rows marked `too_long` go out with the rest
    held = sorted(
        [
            *held_sms_store.list_held(family_id, alert.peerPhone, target_uid, status="held"),
            *held_sms_store.list_held(family_id, alert.peerPhone, target_uid, status="too_long"),
        ],
        key=lambda r: (r.receivedAt, r.id),
    )
    # (a) nothing is written if the member's inbound policy refuses numbers.
    if held and policy_module.rule(target.policy.in_, "external") == "none":
        raise HTTPException(
            status_code=409,
            detail=(
                f"@{target.alias}'s inbound policy does not allow numbers; "
                "change it under People"
            ),
        )

    # (b) the contact.
    try:
        external = externals_store.get_or_create(family_id, alert.peerPhone, req.name)
    except externals_store.ContactNameTaken as exc:
        raise _name_taken(family_id, exc) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    # (c) the edge.
    allow_store.set_edge(target_uid, external.uid, message=True, locate=False)
    allow_store.recompute_locatable_by_for_owner(external.uid)
    # (d) the contact reaches the family's pagers; the member's book changes.
    rederive_family_sms_contacts(family_id, broker)
    book.bump_and_push({target_uid}, broker, reason="sms_approve")

    # (e) the backlog, oldest first.
    delivered = undelivered = 0
    bid = externals_store.ensure_sms_backend(external)
    # The newest held row's channel is recorded as a live inbound would, so the first reply uses it.
    channel_row = next((r for r in reversed(held) if r.via), None)
    if channel_row is not None and channel_row.via is not None:
        inbound_text.record_channel(external.uid, bid, target_uid, channel_row.via, channel_row.conv)
    for row in held:
        pager_text = sms_text.pager_body(row.body)
        if not pager_text:
            held_sms_store.set_status([row.id], "empty")
            continue
        result = routing.send(
            sender_uid=external.uid,
            recipient_alias=target.alias,
            kind="text",
            body=pager_text,
            origin_backend_kind="sms",
            origin_backend_id=bid,
            wire_id=row.id,
            ts=int(row.receivedAt.timestamp()),
        )
        if result.rejected:
            undelivered += 1
            continue
        # a deduplicated send (empty `messages`) was delivered earlier
        held_sms_store.set_status([row.id], "delivered")
        delivered += 1
    return delivered, undelivered


def _approve_new_conversation(alert: Alert) -> None:
    if alert.subjectUid is None or alert.peerUid is None:
        raise HTTPException(status_code=400, detail="alert has no subject/peer to approve")
    allow_store.set_edge(alert.subjectUid, alert.peerUid, message=True, locate=False)
    allow_store.recompute_locatable_by_for_owner(alert.peerUid)


def _approve_contact_request(alert: Alert, req: ApproveAlertRequest, broker: BrokerClient) -> None:
    """The entry already exists, so approving grants the `message` edge (keeping `locate`)."""
    if req.mode == "create":
        raise HTTPException(status_code=400, detail="new people are added under Family > People")
    if alert.contactRequestKey is not None:
        raise HTTPException(status_code=409, detail="superseded; add again from the pager")
    if alert.subjectUid is None or alert.peerUid is None:
        raise HTTPException(status_code=400, detail="alert has no subject/peer to approve")
    owner = users_store.get_user(alert.subjectUid)
    peer = users_store.get_user(alert.peerUid)
    if owner is None or peer is None:
        raise HTTPException(status_code=409, detail="the contact no longer exists")
    existing = allow_store.get_edge(owner.uid, peer.uid)
    allow_store.set_edge(
        owner.uid,
        peer.uid,
        message=True,
        locate=existing.locate if existing is not None else False,
    )
    if peer.kind == "external" and owner.familyId is not None:
        rederive_family_sms_contacts(owner.familyId, broker)
    book.bump_and_push({owner.uid}, broker, reason="contact_approve")


class HeldTextOut(BaseModel):
    id: str
    body: str
    receivedAt: datetime
    status: str


class HeldTextsOut(BaseModel):
    held: list[HeldTextOut]


@router.get("/alerts/{alert_id}/held")
def list_alert_held(alert_id: str, scope: FamilyScope) -> HeldTextsOut:
    """The texts behind an `sms_unknown` alert, oldest first, every status."""
    _, family_id = scope
    alert = alerts_store.get(family_id, alert_id)
    if alert is None:
        raise HTTPException(status_code=404, detail="no such alert")
    if alert.kind != "sms_unknown" or alert.peerPhone is None or alert.subjectUid is None:
        return HeldTextsOut(held=[])
    rows = held_sms_store.list_held(family_id, alert.peerPhone, alert.subjectUid, status=None)
    return HeldTextsOut(
        held=[
            HeldTextOut(id=r.id, body=r.body, receivedAt=r.receivedAt, status=r.status)
            for r in rows
        ]
    )


@router.post("/alerts/{alert_id}/approve", dependencies=[Depends(require_family_write_rate_limit)])
def approve_alert(
    alert_id: str,
    req: ApproveAlertRequest,
    scope: FamilyScope,
    broker: Annotated[BrokerClient, Depends(get_broker)],
    routing: Annotated[Routing, Depends(conversations_router.get_routing)],
) -> Alert | AlertDecision:
    principal, family_id = scope
    alert = _require_open_family_alert(alert_id, family_id)

    if alert.kind == "sms_unknown":
        delivered, undelivered = _approve_sms_unknown(alert, req, family_id, broker, routing)
        decided = alerts_store.decide(family_id, alert_id, "handled", principal.uid)
        return AlertDecision(alert=decided, delivered=delivered, undelivered=undelivered)
    elif alert.kind == "new_conversation":
        _approve_new_conversation(alert)
    elif alert.kind == "chat_unknown":
        raise HTTPException(
            status_code=400, detail="subscribe this conversation under People \u2192 Google Chat"
        )
    elif alert.kind in ("hook_flag", "hook_down") or alert.kind == "fed_moved":
        raise HTTPException(status_code=400, detail="nothing to approve; dismiss it")
    elif alert.kind == "fed_contact":
        from app.routers.fed_admin import approve_fed_contact_alert

        approve_fed_contact_alert(alert, family_id, broker, alias=req.alias, name=req.name)
    elif alert.kind == "fed_unknown":
        raise HTTPException(
            status_code=400, detail="add them under Family > Contacts (a federated person), or dismiss"
        )
    else:  # "contact_request"
        _approve_contact_request(alert, req, broker)

    return alerts_store.decide(family_id, alert_id, "handled", principal.uid)


def _reject_linked_contact_request(
    alert: Alert, reason: str, decided_by: str, broker: BrokerClient
) -> None:
    """Reject the `contactRequests` doc behind a blocked or dismissed `contact_request` alert.

    A no-op if already decided.
    """
    if alert.kind != "contact_request" or alert.contactRequestKey is None:
        return
    request = contacts_store.get_request(alert.contactRequestKey)
    if request is None or request.status != "pending":
        return
    contacts_store.reject(alert.contactRequestKey, reason=reason, decided_by=decided_by)
    contacts_store.bump_book_version(request.deviceId)
    devcfg.push_book(request.deviceId, broker)


def _mark_held(family_id: str, alert: Alert, status: held_sms_store.HeldStatus) -> None:
    """Set `status` on the alert's still-`held` texts; block covers every family member."""
    if alert.peerPhone is None:
        return
    to_uid = None if status == "blocked" else alert.subjectUid
    if status != "blocked" and to_uid is None:
        return
    rows = held_sms_store.list_held(family_id, alert.peerPhone, to_uid, status="held")
    held_sms_store.set_status([r.id for r in rows], status)


@router.post("/alerts/{alert_id}/block", dependencies=[Depends(require_family_write_rate_limit)])
def block_alert(
    alert_id: str,
    scope: FamilyScope,
    broker: Annotated[BrokerClient, Depends(get_broker)],
) -> Alert:
    """Add the alert's `peerPhone` to `families.blockedNumbers` (no-op if it has none)."""
    principal, family_id = scope
    alert = _require_open_family_alert(alert_id, family_id)
    if alert.kind == "chat_unknown":
        raise HTTPException(
            status_code=400, detail="use Ignore for a Google Chat conversation"
        )
    if alert.peerPhone is not None:
        families_store.add_blocked_number(family_id, alert.peerPhone)
        if alert.kind == "sms_unknown":
            _mark_held(family_id, alert, "blocked")
            # decide every other open alert for this number
            for other in alerts_store.list_alerts(family_id, "open"):
                if (
                    other.id != alert.id
                    and other.kind == "sms_unknown"
                    and other.peerPhone == alert.peerPhone
                ):
                    alerts_store.decide(family_id, other.id, "handled", principal.uid)
    _reject_linked_contact_request(alert, "blocked", principal.uid, broker)
    return alerts_store.decide(family_id, alert_id, "handled", principal.uid)


@router.post("/alerts/{alert_id}/dismiss", dependencies=[Depends(require_family_write_rate_limit)])
def dismiss_alert(
    alert_id: str,
    scope: FamilyScope,
    broker: Annotated[BrokerClient, Depends(get_broker)],
) -> Alert:
    principal, family_id = scope
    alert = _require_open_family_alert(alert_id, family_id)
    _reject_linked_contact_request(alert, "dismissed", principal.uid, broker)
    if alert.kind == "sms_unknown":
        _mark_held(family_id, alert, "dismissed")
    if alert.kind == "chat_unknown" and alert.bridgeId and alert.conversationId:
        # the conversation row stays `seen`, so a later text raises a fresh alert
        chat_subscribe.dismiss_alert_conversation(
            family_id, alert.bridgeId, alert.conversationId, principal.uid
        )
    return alerts_store.decide(family_id, alert_id, "dismissed", principal.uid)
