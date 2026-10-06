"""`/api/family/*`: family admin routes (role `admin` in their own family, or `super` naming one).

Member, group and device writes reuse the `app/routers/admin.py` implementations.
"""

from __future__ import annotations

import logging
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from firebase_admin import auth as fb_auth
from google.api_core.exceptions import AlreadyExists

from app import policy as policy_module
from app.auth import Principal, require_family_admin, set_claims
from app.book import rederive_family_sms_contacts, rederive_sms_contacts
from app.broker import BrokerClient
from app.db.firestore import get_db
from app.ingest import Ingest
from app.routers import admin as admin_router
from app.routers import conversations as conversations_router
from app.store import alerts as alerts_store
from app.store import allow as allow_store
from app.store import contacts as contacts_store
from app.store import conversations as conversations_store
from app.store import devices as devices_store
from app.store import externals as externals_store
from app.store import families as families_store
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


    """Fallback-to-a-fresh-instance, like the admin router's `get_emqx`."""
    existing = getattr(request.app.state, "emqx_admin", None)
    if existing is not None:
        return existing


def get_app_settings(request: Request) -> Settings:
    return request.app.state.settings


class FamilyOut(Family):
    memberCount: int
    deviceCount: int


def _family_out(family_id: str) -> FamilyOut:
    family = families_store.get_family(family_id)
    if family is None:
        raise HTTPException(status_code=404, detail="no such family")
    member_count = sum(1 for u in users_store.list_users() if u.familyId == family_id)
    device_count = sum(1 for d in devices_store.list_devices() if d.familyId == family_id)


@router.get("")
def get_family(scope: FamilyScope) -> FamilyOut:
    _, family_id = scope
    return _family_out(family_id)


class PatchFamilyRequest(BaseModel):


@router.patch("", dependencies=[Depends(require_family_write_rate_limit)])
def patch_family(req: PatchFamilyRequest, scope: FamilyScope) -> FamilyOut:
    """Rename the family (web only: no book bump, no push) and/or patch `settings` partially."""
    _, family_id = scope
    try:
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
    settings: Annotated[Settings, Depends(get_app_settings)],
) -> admin_router.DeviceSetupCodeResponse:
    _, family_id = scope
    _require_family_owner(req.ownerAlias, family_id)
    return admin_router._create_device_impl(req, broker, emqx, settings)


@router.post(
    "/devices/{device_id}/rotate-credentials",
    dependencies=[Depends(require_family_write_rate_limit)],
)
def rotate_credentials(
    device_id: str,
    scope: FamilyScope,
    broker: Annotated[BrokerClient, Depends(get_broker)],
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


@router.delete("/devices/{device_id}", dependencies=[Depends(require_family_write_rate_limit)])
    _, family_id = scope
    _require_family_device(device_id, family_id)


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


    people: list[ApprovedPerson] = []
    contacts: list[ApprovedContact] = []
    for edge in allow_store.list_edges():
        if edge.fromUid != uid:
            continue
        peer = users_store.get_user(edge.toUid)
        if peer is None:
            continue
        if peer.kind == "external":
                contacts.append(ApprovedContact(uid=peer.uid, message=edge.message))
        else:
            people.append(
                ApprovedPerson(alias=peer.alias, message=edge.message, locate=edge.locate)
            )
    people.sort(key=lambda p: p.alias)
    contacts.sort(key=lambda c: c.uid)
    return ApprovedOut(people=people, contacts=contacts)


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
        ):
            raise HTTPException(status_code=404, detail=f"no such contact: {c.uid}")

    for peer_uid, p in resolved_people:
        allow_store.set_edge(uid, peer_uid, message=p.message, locate=p.locate)

    kept_contact_uids = {c.uid for c in req.contacts}
    for edge in allow_store.list_edges():
        if edge.fromUid != uid:
            continue
            allow_store.delete_edge(uid, edge.toUid)

    for c in req.contacts:
        allow_store.set_edge(uid, c.uid, message=c.message, locate=False)

    rederive_family_sms_contacts(family_id, broker)
        # `c[]` projects the member's people edges; bump then push for every device `uid` owns
        for device in devices_store.list_devices(owner_uid=uid):
            contacts_store.bump_book_version(device.id)
            devcfg.push_book(device.id, broker)



class ContactOut(BaseModel):
    uid: str
    alias: str
    phone: str | None
    displayName: str
    approvedFor: list[str]
    # Members who hold the contact through their outbound policy alone (open / any_sms).
    impliedFor: list[str] = []


def _name_taken(family_id: str, exc: externals_store.ContactNameTaken) -> HTTPException:
    logger.info("contact name taken family=%s", family_id)
    return HTTPException(status_code=409, detail=str(exc))


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
        )
        for ext in externals_store.list_family_contacts(family_id)
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


def _approve_sms_unknown(
    alert: Alert,
    req: ApproveAlertRequest,
    family_id: str,
    broker: BrokerClient,
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

    try:
        external = externals_store.get_or_create(family_id, alert.peerPhone, req.name)
    except externals_store.ContactNameTaken as exc:
        raise _name_taken(family_id, exc) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    allow_store.set_edge(target_uid, external.uid, message=True, locate=False)
    allow_store.recompute_locatable_by_for_owner(external.uid)
    rederive_family_sms_contacts(family_id, broker)


def _approve_new_conversation(alert: Alert) -> None:
    if alert.subjectUid is None or alert.peerUid is None:
        raise HTTPException(status_code=400, detail="alert has no subject/peer to approve")
    allow_store.set_edge(alert.subjectUid, alert.peerUid, message=True, locate=False)
    allow_store.recompute_locatable_by_for_owner(alert.peerUid)


    if req.mode == "create":
        raise HTTPException(status_code=400, detail="new people are added under Family > People")
        rederive_family_sms_contacts(owner.familyId, broker)
    book.bump_and_push({owner.uid}, broker, reason="contact_approve")


@router.post("/alerts/{alert_id}/approve", dependencies=[Depends(require_family_write_rate_limit)])
def approve_alert(
    alert_id: str,
    req: ApproveAlertRequest,
    scope: FamilyScope,
    broker: Annotated[BrokerClient, Depends(get_broker)],
    principal, family_id = scope
    alert = _require_open_family_alert(alert_id, family_id)

    if alert.kind == "sms_unknown":
    elif alert.kind == "new_conversation":
        _approve_new_conversation(alert)
    else:  # "contact_request"

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


@router.post("/alerts/{alert_id}/block", dependencies=[Depends(require_family_write_rate_limit)])
def block_alert(
    alert_id: str,
    scope: FamilyScope,
    broker: Annotated[BrokerClient, Depends(get_broker)],
) -> Alert:
    """Add the alert's `peerPhone` to `families.blockedNumbers` (no-op if it has none)."""
    principal, family_id = scope
    alert = _require_open_family_alert(alert_id, family_id)
    if alert.peerPhone is not None:
        families_store.add_blocked_number(family_id, alert.peerPhone)
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
    return alerts_store.decide(family_id, alert_id, "dismissed", principal.uid)
