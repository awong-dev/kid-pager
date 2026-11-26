"""Federation management: `/api/admin/fed/*` (super) and `/api/family/fed/*` (family admin).

While the server switch is off every route 404s except the three switch routes."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from app.auth import AuthedUser, require_super
from app.broker import BrokerClient
from app.config import Settings
from app.fed import client as client_mod
from app.fed import contacts as fed_contacts
from app.fed import keys, linking
from app.fed.client import FedClient
from app.fed.deps import app_settings
from app.routers.admin import require_admin_write_rate_limit
from app.routers.family import FamilyScope, get_broker, require_family_write_rate_limit
from app.store import externals as externals_store
from app.store import families as families_store
from app.store import fed_families, fed_links, fed_outbox, fed_state
from app.store import users as users_store

logger = logging.getLogger("relay.fed.admin")


def require_server_on() -> None:
    if not fed_state.server_enabled():
        raise HTTPException(status_code=404, detail="Not Found")


admin_router = APIRouter(
    prefix="/api/admin/fed",
    dependencies=[Depends(require_server_on), Depends(require_super)],
)
# The switches themselves: super only, reachable in every state.
state_router = APIRouter(prefix="/api/admin/fed", dependencies=[Depends(require_super)])


def require_family_federation(scope: FamilyScope) -> None:
    """The family routes exist only for an effective family (server and family on)."""
    if not fed_state.family_enabled(scope[1]):
        raise HTTPException(status_code=404, detail="Not Found")


family_router = APIRouter(
    prefix="/api/family/fed",
    dependencies=[Depends(require_server_on), Depends(require_family_federation)],
)


class RemoteOut(BaseModel):
    fedId: str
    name: str
    host: str
    port: int
    path: str  # where the relay is mounted on that host: "" or "/pager"
    fingerprint: str  # of the remote's public key, for the admins to compare out of band


class HealthOut(BaseModel):
    state: str
    since: int


class LinkOut(BaseModel):
    id: str
    remote: RemoteOut
    state: str
    shared: list[str]  # this family's members the remote family may see
    health: HealthOut
    pending: int  # outbox rows waiting for the link
    announcePending: bool  # this family moved here and the other side has not heard yet
    remoteKeyChanged: bool  # a reauthorization was accepted on the code alone: compare keys
    createdAt: datetime | None = None
    lastOkAt: datetime | None = None


class AdminLinkOut(LinkOut):
    familyId: str
    familyName: str


def _pending_counts() -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in fed_outbox.list_pending():
        counts[row.linkId] = counts.get(row.linkId, 0) + 1
    return counts


def link_out(link: fed_links.FedLink, pending: dict[str, int] | None = None) -> LinkOut:
    return LinkOut(
        id=link.id,
        remote=RemoteOut(
            fedId=link.remote.fedId,
            name=link.remote.name,
            host=link.remote.host,
            port=link.remote.port,
            path=link.remote.path,
            fingerprint=keys.fingerprint(link.remote.publicKey),
        ),
        state=link.state,
        shared=link.shared,
        health=HealthOut(state=link.health.state, since=link.health.since),
        pending=(pending if pending is not None else _pending_counts()).get(link.id, 0),
        announcePending=link.announce_pending,
        remoteKeyChanged=link.remoteKeyChanged,
        createdAt=link.createdAt,
        lastOkAt=link.lastOkAt,
    )


def _refuse_reauth_state(link: fed_links.FedLink) -> None:
    """Suspend / resume must not turn a `reauth` link (restored from a backup) into an active one."""
    if link.state == "reauth":
        raise HTTPException(status_code=409, detail="reauthorize this link first")


def _family_name(family_id: str) -> str:
    family = families_store.get_family(family_id)
    return family.name if family else family_id


def get_fed_client() -> FedClient:
    return client_mod.get_client()


class FamilySwitchOut(BaseModel):
    id: str
    name: str
    enabled: bool


class StateOut(BaseModel):
    enabled: bool
    enabledAt: datetime | None = None
    families: list[FamilySwitchOut]


class SwitchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool


def _state_out() -> StateOut:
    stored = fed_state.server_state()
    return StateOut(
        enabled=stored.enabled,
        enabledAt=stored.enabledAt if stored.enabled else None,
        families=[
            FamilySwitchOut(id=f.id, name=f.name, enabled=f.federation)
            for f in sorted(families_store.list_families(), key=lambda f: (f.name, f.id))
        ],
    )


@state_router.get("/state")
def get_state() -> StateOut:
    return _state_out()


@state_router.put("/state", dependencies=[Depends(require_admin_write_rate_limit)])
def put_state(
    req: SwitchRequest, authed: Annotated[AuthedUser, Depends(require_super)]
) -> StateOut:
    fed_state.set_server_enabled(req.enabled, authed.uid)
    return _state_out()


@state_router.put(
    "/families/{family_id}", dependencies=[Depends(require_admin_write_rate_limit)]
)
def put_family_state(family_id: str, req: SwitchRequest) -> StateOut:
    """Allowed while the server switch is off (pre-arming)."""
    try:
        fed_state.set_family_enabled(family_id, req.enabled)
    except KeyError:
        raise HTTPException(status_code=404, detail="no such family") from None
    return _state_out()


def _require_any_link(link_id: str) -> fed_links.FedLink:
    link = fed_links.get(link_id)
    if link is None:
        raise HTTPException(status_code=404, detail="no such link")
    return link


@admin_router.get("/links")
def admin_links() -> list[AdminLinkOut]:
    pending = _pending_counts()
    names: dict[str, str] = {}
    out: list[AdminLinkOut] = []
    for link in fed_links.list_all():
        if link.familyId not in names:
            names[link.familyId] = _family_name(link.familyId)
        out.append(
            AdminLinkOut(
                **link_out(link, pending).model_dump(),
                familyId=link.familyId,
                familyName=names[link.familyId],
            )
        )
    return out


def _admin_link_out(link: fed_links.FedLink) -> AdminLinkOut:
    return AdminLinkOut(
        **link_out(link).model_dump(),
        familyId=link.familyId,
        familyName=_family_name(link.familyId),
    )


@admin_router.post(
    "/links/{link_id}/suspend", dependencies=[Depends(require_admin_write_rate_limit)]
)
def admin_suspend(link_id: str) -> AdminLinkOut:
    _refuse_reauth_state(_require_any_link(link_id))
    return _admin_link_out(fed_links.set_state(link_id, "suspended"))


@admin_router.post(
    "/links/{link_id}/resume", dependencies=[Depends(require_admin_write_rate_limit)]
)
def admin_resume(link_id: str) -> AdminLinkOut:
    _refuse_reauth_state(_require_any_link(link_id))
    return _admin_link_out(fed_links.set_state(link_id, "active"))


def _refuse_frozen(family_id: str) -> None:
    """A frozen family's links belong to its exported copy; an `unlink` would break them."""
    if families_store.is_frozen(family_id):
        raise HTTPException(
            status_code=409, detail="family is frozen for a move; links cannot be deleted"
        )


@admin_router.delete(
    "/links/{link_id}", dependencies=[Depends(require_admin_write_rate_limit)]
)
def admin_delete(link_id: str) -> dict[str, int]:
    link = _require_any_link(link_id)
    _refuse_frozen(link.familyId)
    return {"failedRows": linking.delete_link(link, notify=True)}


class FamilyFedOut(BaseModel):
    fedId: str
    name: str
    fingerprint: str
    links: list[LinkOut]


def _family_fed_out(family_id: str) -> FamilyFedOut:
    identity = fed_families.get_or_create(family_id)
    pending = _pending_counts()
    return FamilyFedOut(
        fedId=identity.fedId,
        name=_family_name(family_id),
        fingerprint=identity.fingerprint(),
        links=[link_out(link, pending) for link in fed_links.list_for_family(family_id)],
    )


@family_router.get("")
def get_family_fed(scope: FamilyScope) -> FamilyFedOut:
    """The family's identity (created on first use) and its links."""
    _, family_id = scope
    return _family_fed_out(family_id)


class InviteOut(BaseModel):
    invite: str
    expiresAt: int


@family_router.post(
    "/invites", status_code=201, dependencies=[Depends(require_family_write_rate_limit)]
)
def create_invite(
    scope: FamilyScope, settings: Annotated[Settings, Depends(app_settings)]
) -> InviteOut:
    principal, family_id = scope
    identity = fed_families.get_or_create(family_id)
    try:
        blob, expires = linking.new_invite(settings, identity, principal.uid)
    except linking.LinkingError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail) from exc
    return InviteOut(invite=blob, expiresAt=expires)


class AcceptRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    invite: str = Field(min_length=1, max_length=8192)
    # a reauthorization invite for an existing link changes nothing until this is true
    confirmReauth: bool = False


@family_router.post(
    "/links", status_code=201, dependencies=[Depends(require_family_write_rate_limit)]
)
def accept_invite(
    req: AcceptRequest,
    scope: FamilyScope,
    settings: Annotated[Settings, Depends(app_settings)],
    client: Annotated[FedClient, Depends(get_fed_client)],
) -> LinkOut:
    principal, family_id = scope
    identity = fed_families.get_or_create(family_id)
    try:
        link = linking.accept_invite(
            settings,
            identity,
            req.invite,
            principal.uid,
            client,
            confirm_reauth=req.confirmReauth,
        )
    except linking.ReauthConfirm as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "reauth_confirm",
                "link": link_out(exc.link).model_dump(mode="json"),
                "newFingerprint": keys.fingerprint(exc.blob.pk),
                "newHost": exc.blob.host,
                "newPort": exc.blob.port,
                "newPath": exc.blob.path,
            },
        ) from exc
    except linking.LinkingError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail) from exc
    logger.info("fed linked family=%s with %s", family_id, link.remote.fedId)
    return link_out(link)


def _require_link(family_id: str, link_id: str) -> fed_links.FedLink:
    link = fed_links.get(link_id)
    if link is None or link.familyId != family_id:
        raise HTTPException(status_code=404, detail="no such link")
    return link


@family_router.post(
    "/links/{link_id}/suspend", dependencies=[Depends(require_family_write_rate_limit)]
)
def suspend_link(link_id: str, scope: FamilyScope) -> LinkOut:
    _, family_id = scope
    _refuse_reauth_state(_require_link(family_id, link_id))
    return link_out(fed_links.set_state(link_id, "suspended"))


@family_router.post(
    "/links/{link_id}/resume", dependencies=[Depends(require_family_write_rate_limit)]
)
def resume_link(link_id: str, scope: FamilyScope) -> LinkOut:
    _, family_id = scope
    _refuse_reauth_state(_require_link(family_id, link_id))
    return link_out(fed_links.set_state(link_id, "active"))


@family_router.delete(
    "/links/{link_id}", dependencies=[Depends(require_family_write_rate_limit)]
)
def delete_link(link_id: str, scope: FamilyScope) -> dict[str, int]:
    """Unlink: pending outbox rows fail `unpeered`, the row goes, the other family is told."""
    _, family_id = scope
    link = _require_link(family_id, link_id)
    _refuse_frozen(family_id)
    return {"failedRows": linking.delete_link(link, notify=True)}


class ReauthOut(BaseModel):
    invite: str
    url: str | None  # on the other family's web app, the invite in the fragment; null if unknown
    expiresAt: int


@family_router.post(
    "/links/{link_id}/reauth",
    status_code=201,
    dependencies=[Depends(require_family_write_rate_limit)],
)
def reauthorize_link(
    link_id: str,
    scope: FamilyScope,
    settings: Annotated[Settings, Depends(app_settings)],
    client: Annotated[FedClient, Depends(get_fed_client)],
) -> ReauthOut:
    """Make an invite for a `reauth` link (a family restored from a backup)."""
    principal, family_id = scope
    link = _require_link(family_id, link_id)
    if link.state != "reauth":
        raise HTTPException(status_code=409, detail="this link does not need reauthorization")
    identity = fed_families.get_or_create(family_id)
    try:
        invite, url, expires = linking.new_reauth_invite(
            settings, identity, link, principal.uid, client
        )
    except linking.LinkingError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail) from exc
    return ReauthOut(invite=invite, url=url, expiresAt=expires)


class ShareRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    shared: list[str]


@family_router.put(
    "/links/{link_id}/shared", dependencies=[Depends(require_family_write_rate_limit)]
)
def put_shared(link_id: str, req: ShareRequest, scope: FamilyScope) -> LinkOut:
    """Full replacement of which of this family's members the other family may see."""
    _, family_id = scope
    _require_link(family_id, link_id)
    for uid in set(req.shared):
        user = users_store.get_user(uid)
        if (
            user is None
            or user.kind != "person"
            or user.familyId != family_id
            or user.disabled
        ):
            raise HTTPException(status_code=404, detail=f"no such member: {uid}")
    return link_out(fed_links.set_shared(link_id, req.shared))


class DirectoryItem(BaseModel):
    rid: str
    alias: str
    name: str
    added: bool  # this family already has an external for the person


def _remote_error(exc: client_mod.FedError) -> HTTPException:
    return HTTPException(status_code=502, detail=exc.code)


def _active_link_and_identity(
    family_id: str, link_id: str
) -> tuple[fed_links.FedLink, fed_families.FedFamily]:
    link = _require_link(family_id, link_id)
    if link.state != "active":
        raise HTTPException(status_code=409, detail="link is suspended")
    identity = fed_families.get(family_id)
    if identity is None:
        raise HTTPException(status_code=404, detail="no such link")
    return link, identity


@family_router.get("/links/{link_id}/directory")
def link_directory(
    link_id: str,
    scope: FamilyScope,
    client: Annotated[FedClient, Depends(get_fed_client)],
) -> list[DirectoryItem]:
    _, family_id = scope
    link, identity = _active_link_and_identity(family_id, link_id)
    try:
        entries = client.directory(identity, link)
    except client_mod.FedError as exc:
        raise _remote_error(exc) from exc
    return [
        DirectoryItem(
            rid=e.rid,
            alias=e.alias,
            name=e.name,
            added=externals_store.get_family_fed(family_id, link.remote.fedId, e.rid)
            is not None,
        )
        for e in entries
    ]


class AddRemoteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rid: str = Field(min_length=1, max_length=128)
    members: list[str] = Field(min_length=1)
    alias: str | None = Field(default=None, max_length=16)
    name: str | None = Field(default=None, min_length=1, max_length=120)


class RemoteContactOut(BaseModel):
    uid: str
    alias: str
    displayName: str
    linkId: str
    rid: str
    members: list[str]


@family_router.post(
    "/links/{link_id}/contacts",
    status_code=201,
    dependencies=[Depends(require_family_write_rate_limit)],
)
def add_remote_contact(
    link_id: str,
    req: AddRemoteRequest,
    scope: FamilyScope,
    broker: Annotated[BrokerClient, Depends(get_broker)],
    client: Annotated[FedClient, Depends(get_fed_client)],
) -> RemoteContactOut:
    """Add a person from the link's directory as a contact; 409 `alias_taken` / `name_taken`."""
    _, family_id = scope
    link, identity = _active_link_and_identity(family_id, link_id)
    try:
        members = fed_contacts.check_members(family_id, link, req.members)
    except fed_contacts.NotAMember as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except fed_contacts.NotShared as exc:
        raise HTTPException(status_code=409, detail=f"not_shared: {exc}") from exc
    try:
        entries = client.directory(identity, link)
    except client_mod.FedError as exc:
        raise _remote_error(exc) from exc
    entry = next((e for e in entries if e.rid == req.rid), None)
    if entry is None:
        raise HTTPException(
            status_code=404, detail="that person is not shared by the other family"
        )
    try:
        external = fed_contacts.add_remote_person(
            family_id=family_id,
            link=link,
            rid=entry.rid,
            remote_alias=entry.alias,
            remote_name=entry.name,
            members=members,
            broker=broker,
            alias=req.alias,
            name=req.name,
        )
    except externals_store.ContactNameTaken as exc:
        raise HTTPException(status_code=409, detail=f"name_taken: {exc}") from exc
    except users_store.AliasTaken as exc:
        raise HTTPException(status_code=409, detail="alias_taken") from exc
    except users_store.InvalidAlias as exc:
        raise HTTPException(status_code=400, detail=f"invalid_alias: {exc}") from exc
    return RemoteContactOut(
        uid=external.uid,
        alias=external.alias,
        displayName=external.displayName,
        linkId=link.id,
        rid=entry.rid,
        members=[m.uid for m in members],
    )


def approve_fed_contact_alert(
    alert, family_id: str, broker: BrokerClient, *, alias: str | None, name: str | None
) -> None:
    """Add back the person who added the member, for a `fed_contact` alert; 404 unless enabled."""
    if not fed_state.family_enabled(family_id):
        raise HTTPException(status_code=404, detail="Not Found")
    if (
        not alert.subjectUid
        or not alert.fedLinkId
        or not alert.fedRid
        or not alert.peerAlias
    ):
        raise HTTPException(status_code=400, detail="alert has no person to add")
    link = _require_link(family_id, alert.fedLinkId)
    if link.state != "active":
        raise HTTPException(status_code=409, detail="link is suspended")
    try:
        members = fed_contacts.check_members(family_id, link, [alert.subjectUid])
    except fed_contacts.NotAMember as exc:
        raise HTTPException(status_code=403, detail="target is not a member of this family") from exc
    except fed_contacts.NotShared as exc:
        raise HTTPException(status_code=409, detail=f"not_shared: {exc}") from exc
    try:
        fed_contacts.add_remote_person(
            family_id=family_id,
            link=link,
            rid=alert.fedRid,
            remote_alias=alert.peerAlias,
            remote_name=alert.peerName or alert.peerAlias,
            members=members,
            broker=broker,
            alias=alias,
            name=name,
        )
    except externals_store.ContactNameTaken as exc:
        raise HTTPException(status_code=409, detail=f"name_taken: {exc}") from exc
    except users_store.AliasTaken as exc:
        raise HTTPException(status_code=409, detail="alias_taken") from exc
    except users_store.InvalidAlias as exc:
        raise HTTPException(status_code=400, detail=f"invalid_alias: {exc}") from exc
