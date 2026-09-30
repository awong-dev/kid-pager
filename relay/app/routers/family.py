"""`/api/family/*`: family admin routes (role `admin` in their own family, or `super` naming one).

Member, group and device writes reuse the `app/routers/admin.py` implementations.
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from firebase_admin import auth as fb_auth
from google.api_core.exceptions import AlreadyExists

from app import policy as policy_module
from app.auth import Principal, require_family_admin, set_claims
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
from app.store.families import Family
from app.store.users import InvalidAlias, Policy, User

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


    family = families_store.get_family(family_id)
    if family is None:
        raise HTTPException(status_code=404, detail="no such family")
    member_count = sum(1 for u in users_store.list_users() if u.familyId == family_id)
    device_count = sum(1 for d in devices_store.list_devices() if d.familyId == family_id)


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
    """Same flow as the admin `create_user`, with the scope family fixed."""
    _, family_id = scope
        raise HTTPException(status_code=400, detail="email or phone is required")


    try:
        user = users_store.create_user(
            alias=req.alias,
            display_name=req.displayName,
            email=req.email,
            role=req.role,
            family_id=family_id,
        )
    except (users_store.AliasTaken, users_store.InvalidAlias) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    set_claims(user.uid, user.role, user.familyId)
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

        uid,
        display_name=req.displayName,
        email=None,
        phone=None,
        role=req.role,
        family_id=None,
        disabled=req.disabled,
        broker=broker,
    )




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




class PutApprovedRequest(BaseModel):
    people: list[ApprovedPerson] = []


class ApprovedOut(BaseModel):
    people: list[ApprovedPerson]


@router.put("/members/{uid}/approved", dependencies=[Depends(require_family_write_rate_limit)])
def put_approved(
    uid: str,
    req: PutApprovedRequest,
    scope: FamilyScope,
    broker: Annotated[BrokerClient, Depends(get_broker)],
) -> ApprovedOut:
    _, family_id = scope

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


    for peer_uid, p in resolved_people:
        allow_store.set_edge(uid, peer_uid, message=p.message, locate=p.locate)

    for edge in allow_store.list_edges():
        if edge.fromUid != uid:
            continue
            allow_store.delete_edge(uid, edge.toUid)





class ContactOut(BaseModel):
    uid: str
    alias: str
    phone: str | None
    displayName: str
    approvedFor: list[str]




    approved_for: dict[str, set[str]] = {}
    for edge in allow_store.list_edges():

    out = [
        ContactOut(
            uid=ext.uid,
            alias=ext.alias,
            displayName=ext.displayName,
        )
    ]
    out.sort(key=lambda c: c.displayName.casefold())
    return out


class CreateContactRequest(BaseModel):
    phone: str
    name: str = Field(min_length=1)


@router.post(
    "/contacts", status_code=201, dependencies=[Depends(require_family_write_rate_limit)]
)
    try:
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


class PatchContactRequest(BaseModel):
    name: str = Field(min_length=1)


@router.patch("/contacts/{uid}", dependencies=[Depends(require_family_write_rate_limit)])
    _, family_id = scope


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
    mode: Literal["link", "create"] | None = None
    alias: str | None = None


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

    allow_store.set_edge(target_uid, external.uid, message=True, locate=False)
    allow_store.recompute_locatable_by_for_owner(external.uid)


def _approve_new_conversation(alert: Alert) -> None:
    if alert.subjectUid is None or alert.peerUid is None:
        raise HTTPException(status_code=400, detail="alert has no subject/peer to approve")
    allow_store.set_edge(alert.subjectUid, alert.peerUid, message=True, locate=False)
    allow_store.recompute_locatable_by_for_owner(alert.peerUid)


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
