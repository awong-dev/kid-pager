"""`/api/family/bridges*` and `/api/family/members/{uid}/chat`: bridge and Google Chat admin.

Family admin (or super naming a family); a bridge of another family is a 404.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from app import bridge_health, bridge_numbers, chat_subscribe, deliveries, devcfg
from app.auth import AuthedUser, require_user
from app.broker import BrokerClient
from app.carriers import service as carriers_service
from app.chat_subscribe import ChatError
from app.routers.bridge import normalize_optional_phone
from app.routers.family import (
    FamilyScope,
    _require_family_member,
    require_family_write_rate_limit,
)
from app.routing import Routing
from app.store import bridge_conversations, bridge_outbox
from app.store import bridges as bridges_store
from app.store import conversations as conversations_store
from app.store import devices as devices_store
from app.store import users as users_store
from app.store.bridge_conversations import BridgeConversation

logger = logging.getLogger("relay.family_bridges")

router = APIRouter(prefix="/api/family")
# `GET /api/me/bridges`: a member's own bridge phones (any signed-in member, not an admin route).
me_router = APIRouter(prefix="/api/me")


def get_broker(request: Request) -> BrokerClient:
    return request.app.state.broker


def get_routing(request: Request) -> Routing:
    return request.app.state.routing


def _chat_error(exc: ChatError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=exc.detail)


def _require_bridge(bridge_id: str, family_id: str) -> bridges_store.Bridge:
    bridge = bridges_store.get(bridge_id)
    if bridge is None or bridge.familyId != family_id:
        raise HTTPException(status_code=404, detail="no such bridge")
    return bridge


class RosterEntry(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str = Field(min_length=1, max_length=200)
    nick: str = Field(max_length=64)


class SubscribeRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    pagerName: str = Field(max_length=200)
    canReply: bool = True
    roster: list[RosterEntry] = Field(default_factory=list, max_length=64)


class PatchConversationRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    pagerName: str | None = Field(default=None, max_length=200)
    canReply: bool | None = None
    paused: bool | None = None
    roster: list[RosterEntry] | None = Field(default=None, max_length=64)


class ConvHealthOut(BaseModel):
    state: str
    okAt: int | None
    failAt: int | None
    reason: str | None


class ConversationOut(BaseModel):
    ref: str
    bridgeId: str
    conversationId: str
    source: str
    title: str | None
    isGroup: bool
    link: str | None
    people: list[str]
    lastPreview: str
    lastAt: datetime | None
    firstSeenAt: datetime | None
    inspectedAt: datetime | None
    heldCount: int
    status: str
    uid: str | None
    convKey: str | None
    pagerName: str | None
    customName: bool
    alertId: str | None
    # `{state: ok|failing|unreachable, okAt, failAt, reason}` (epoch seconds); null until seen.
    health: ConvHealthOut | None = None
    # Subscribed rows only: the alias the pager addresses, whether the kid can
    # reply, the roster (group) and whether it is within the pager's first 32.
    alias: str | None = None
    canReply: bool | None = None
    roster: list[RosterEntry] | None = None
    onPager: bool | None = None


class ChatTabOut(BaseModel):
    subscribed: list[ConversationOut]
    seen: list[ConversationOut]
    bridges: list[dict[str, Any]]


class SubscribeOut(BaseModel):
    uid: str
    convKey: str | None
    alias: str
    delivered: int
    undelivered: int
    conversation: ConversationOut


def _row_out(row: BridgeConversation, *, on_pager: set[str] | None = None) -> ConversationOut:
    out = ConversationOut(
        ref=row.ref,
        bridgeId=row.bridgeId,
        conversationId=row.conversationId,
        source=row.source,
        title=row.title,
        isGroup=row.isGroup,
        link=row.link,
        people=row.people,
        lastPreview=row.lastPreview,
        lastAt=row.lastAt,
        firstSeenAt=row.firstSeenAt,
        inspectedAt=row.inspectedAt,
        heldCount=row.heldCount,
        status=row.status,
        uid=row.uid,
        convKey=row.convKey,
        pagerName=row.pagerName,
        customName=row.customName,
        alertId=row.alertId,
        health=(
            ConvHealthOut(
                state=row.health.state,
                okAt=row.health.okAt,
                failAt=row.health.failAt,
                reason=row.health.reason,
            )
            if row.health is not None
            else None
        ),
    )
    if row.status in ("subscribed", "paused") and row.uid:
        ext = users_store.get_user(row.uid)
        out.alias = ext.alias if ext is not None else None
        out.canReply = (ext.chat or {}).get("canReply") is not False if ext is not None else None
        if row.isGroup and row.convKey:
            group = conversations_store.get_bridge_group(row.convKey)
            if group is not None:
                out.alias = group.alias
                out.roster = [RosterEntry(name=n, nick=k) for k, n in sorted(group.roster.items())]
        if on_pager is not None:
            out.onPager = out.alias in on_pager
    return out


def _on_pager_aliases(owner_uid: str) -> set[str]:
    devices = sorted(devices_store.list_devices(owner_uid=owner_uid), key=lambda d: d.id)
    default_alias = devcfg._default_alias(devices[0]) if devices else None
    ordered = devcfg._ordered_contacts(owner_uid, default_alias)
    return {c["a"] for c in ordered[: devcfg.MAX_PULL_CONTACTS]}


@router.get("/members/{uid}/chat")
def member_chat(uid: str, scope: FamilyScope) -> ChatTabOut:
    """The Google Chat tab for member `uid`: subscribed (and paused)
    conversations with `onPager`, and the `seen` / `ignored` ones."""
    _, family_id = scope
    _require_family_member(uid, family_id)
    rows = bridge_conversations.list_for_owner(uid)
    on_pager = _on_pager_aliases(uid)
    return ChatTabOut(
        subscribed=[
            _row_out(r, on_pager=on_pager) for r in rows if r.status in ("subscribed", "paused")
        ],
        seen=[_row_out(r) for r in rows if r.status in ("seen", "ignored")],
        bridges=[
            {"id": b.id, "label": b.label, "paired": b.paired, "caps": b.caps.model_dump()}
            for b in bridges_store.list_for_owner(uid)
            if b.familyId == family_id
        ],
    )


def _row_or_404(bridge: bridges_store.Bridge, ref: str) -> BridgeConversation:
    row = bridge_conversations.get_by_ref(bridge.id, ref)
    if row is None:
        raise HTTPException(status_code=404, detail="no such conversation")
    return row


@router.post(
    "/bridges/{bridge_id}/conversations/{ref}/subscribe",
    dependencies=[Depends(require_family_write_rate_limit)],
)
def subscribe_conversation(
    bridge_id: str,
    ref: str,
    req: SubscribeRequest,
    scope: FamilyScope,
    broker: Annotated[BrokerClient, Depends(get_broker)],
    routing: Annotated[Routing, Depends(get_routing)],
) -> SubscribeOut:
    principal, family_id = scope
    bridge = _require_bridge(bridge_id, family_id)
    row = _row_or_404(bridge, ref)
    try:
        result = chat_subscribe.subscribe(
            family_id,
            bridge,
            row.conversationId,
            req.pagerName,
            req.canReply,
            [(r.name, r.nick) for r in req.roster],
            principal.uid,
            broker,
            routing,
        )
    except ChatError as exc:
        raise _chat_error(exc) from exc
    fresh = bridge_conversations.get_by_ref(bridge.id, ref)
    assert fresh is not None
    return SubscribeOut(
        uid=result.uid,
        convKey=result.convKey,
        alias=result.alias,
        delivered=result.delivered,
        undelivered=result.undelivered,
        conversation=_row_out(fresh, on_pager=_on_pager_aliases(bridge.ownerUid)),
    )


@router.post(
    "/bridges/{bridge_id}/conversations/{ref}/ignore",
    dependencies=[Depends(require_family_write_rate_limit)],
)
def ignore_conversation(bridge_id: str, ref: str, scope: FamilyScope) -> ConversationOut:
    principal, family_id = scope
    bridge = _require_bridge(bridge_id, family_id)
    row = _row_or_404(bridge, ref)
    try:
        fresh = chat_subscribe.ignore(family_id, bridge, row.conversationId, principal.uid)
    except ChatError as exc:
        raise _chat_error(exc) from exc
    return _row_out(fresh)


@router.patch(
    "/bridges/{bridge_id}/conversations/{ref}",
    dependencies=[Depends(require_family_write_rate_limit)],
)
def patch_conversation(
    bridge_id: str,
    ref: str,
    req: PatchConversationRequest,
    scope: FamilyScope,
    broker: Annotated[BrokerClient, Depends(get_broker)],
) -> ConversationOut:
    _, family_id = scope
    bridge = _require_bridge(bridge_id, family_id)
    row = _row_or_404(bridge, ref)
    try:
        fresh = chat_subscribe.patch(
            family_id,
            bridge,
            row.conversationId,
            pager_name=req.pagerName,
            can_reply=req.canReply,
            paused=req.paused,
            roster=None if req.roster is None else [(r.name, r.nick) for r in req.roster],
            broker=broker,
        )
    except ChatError as exc:
        raise _chat_error(exc) from exc
    return _row_out(fresh, on_pager=_on_pager_aliases(bridge.ownerUid))


@router.delete(
    "/bridges/{bridge_id}/conversations/{ref}",
    dependencies=[Depends(require_family_write_rate_limit)],
)
def unsubscribe_conversation(
    bridge_id: str,
    ref: str,
    scope: FamilyScope,
    broker: Annotated[BrokerClient, Depends(get_broker)],
) -> ConversationOut:
    _, family_id = scope
    bridge = _require_bridge(bridge_id, family_id)
    row = _row_or_404(bridge, ref)
    try:
        fresh = chat_subscribe.unsubscribe(family_id, bridge, row.conversationId, broker)
    except ChatError as exc:
        raise _chat_error(exc) from exc
    return _row_out(fresh)


GOOGLE_LINK_PREFIXES = (
    "https://chat.google.com/",
    "https://mail.google.com/chat/",
    "https://voice.google.com/",
)
WHATSAPP_LINK_PREFIXES = (
    "https://wa.me/",
    "http://wa.me/",
    "https://api.whatsapp.com/",
    "https://chat.whatsapp.com/",
    "https://whatsapp.com/",
    "https://www.whatsapp.com/",
    "whatsapp://",
)


class InspectRequest(BaseModel):
    link: str = Field(max_length=2048)


@router.post(
    "/bridges/{bridge_id}/inspect",
    status_code=202,
    dependencies=[Depends(require_family_write_rate_limit)],
)
def inspect_link(bridge_id: str, req: InspectRequest, scope: FamilyScope) -> dict[str, str]:
    """Ask the phone to open a pasted Chat / Voice link and report it; 202 `{outboxId}`."""
    _, family_id = scope
    bridge = _require_bridge(bridge_id, family_id)
    link = req.link.strip()
    lowered = link.lower()
    if lowered.startswith(WHATSAPP_LINK_PREFIXES) or "//wa.me/" in lowered or ".whatsapp.com/" in lowered:
        raise HTTPException(
            status_code=400, detail="whatsapp links cannot be inspected; wait for a message"
        )
    if not link.startswith(GOOGLE_LINK_PREFIXES):
        raise HTTPException(status_code=422, detail="not a Google Chat or Google Voice link")
    if not bridge.paired:
        raise HTTPException(status_code=409, detail="the bridge phone is not paired")
    item = bridge_outbox.enqueue_inspect(bridge, link)
    return {"outboxId": item.id}


class HealthOut(BaseModel):
    state: str
    since: int
    flags: list[str]
    notifiedAt: int | None


class BridgeOut(BaseModel):
    """A bridge as the web sees it: never `tokenHash` or `fcmToken`."""

    id: str
    ownerUid: str
    ownerAlias: str | None
    ownerName: str | None
    label: str
    paired: bool
    pairedAt: datetime | None
    lastSeenAt: datetime | None
    simNumber: str | None
    voiceNumber: str | None
    status: bridges_store.BridgeStatus
    caps: bridges_store.BridgeCaps
    createdAt: datetime | None
    # `{state: ok|offline|degraded, since, flags[], notifiedAt}` (epoch s); null until evaluated.
    health: HealthOut | None = None
    # carrier, texts left and where the number comes from
    carrier: carriers_service.CarrierView | None = None
    # Only on `PATCH` reassign: subscribed Chat conversations unsubscribed by the owner change.
    unsubscribed: int = 0


class CreateBridgeRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    ownerUid: str
    label: str = Field(min_length=1, max_length=80)


class PairCodeOut(BaseModel):
    code: str
    expiresAt: datetime


class CreateBridgeOut(PairCodeOut):
    bridge: BridgeOut


class PatchBridgeRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    ownerUid: str | None = None
    label: str | None = Field(default=None, min_length=1, max_length=80)
    simNumber: str | None = Field(default=None, max_length=32)
    voiceNumber: str | None = Field(default=None, max_length=32)


def _bridge_out(bridge: bridges_store.Bridge) -> BridgeOut:
    owner = users_store.get_user(bridge.ownerUid)
    return BridgeOut(
        id=bridge.id,
        ownerUid=bridge.ownerUid,
        ownerAlias=owner.alias if owner is not None else None,
        ownerName=owner.displayName if owner is not None else None,
        label=bridge.label,
        paired=bridge.paired,
        pairedAt=bridge.pairedAt,
        lastSeenAt=bridge.lastSeenAt,
        simNumber=bridge.simNumber,
        voiceNumber=bridge.voiceNumber,
        status=bridge.status,
        caps=bridge.caps,
        createdAt=bridge.createdAt,
        health=(
            HealthOut(
                state=bridge.health.state,
                since=bridge.health.since,
                flags=bridge.health.flags,
                notifiedAt=bridge.health.notifiedAt,
            )
            if bridge.health is not None
            else None
        ),
        carrier=carriers_service.view(bridge) if bridge.paired else None,
    )


def _require_active_person(uid: str, family_id: str) -> users_store.User:
    owner = users_store.get_user(uid)
    if owner is None or owner.kind != "person" or owner.familyId != family_id:
        raise HTTPException(status_code=404, detail="no such member in this family")
    if owner.disabled:
        raise HTTPException(status_code=409, detail="that member is disabled")
    return owner


class MyBridgeOut(BaseModel):
    """The slice of a bridge its owner may see: enough to render the top-up block."""

    id: str
    label: str
    paired: bool
    health: HealthOut | None = None
    carrier: carriers_service.CarrierView | None = None


@me_router.get("/bridges")
def list_my_bridges(authed: Annotated[AuthedUser, Depends(require_user)]) -> list[MyBridgeOut]:
    """Bridges whose `ownerUid` is the caller."""
    out: list[MyBridgeOut] = []
    for b in bridges_store.list_for_owner(authed.uid):
        full = _bridge_out(b)
        out.append(
            MyBridgeOut(
                id=full.id,
                label=full.label,
                paired=full.paired,
                health=full.health,
                carrier=full.carrier,
            )
        )
    return out


@router.get("/bridges")
def list_bridges(scope: FamilyScope) -> list[BridgeOut]:
    _, family_id = scope
    return [_bridge_out(b) for b in bridges_store.list_for_family(family_id)]


@router.post(
    "/bridges", status_code=201, dependencies=[Depends(require_family_write_rate_limit)]
)
def create_bridge(req: CreateBridgeRequest, scope: FamilyScope) -> CreateBridgeOut:
    """Create the unpaired row and a 10-minute pairing code."""
    principal, family_id = scope
    _require_active_person(req.ownerUid, family_id)
    bridge = bridges_store.create(req.ownerUid, family_id, req.label.strip(), principal.uid)
    code, expires_at = bridges_store.create_pair_code(bridge.id)
    return CreateBridgeOut(bridge=_bridge_out(bridge), code=code, expiresAt=expires_at)


@router.post(
    "/bridges/{bridge_id}/code", dependencies=[Depends(require_family_write_rate_limit)]
)
def new_pair_code(bridge_id: str, scope: FamilyScope) -> PairCodeOut:
    """A new code, only while the bridge has no token."""
    _, family_id = scope
    bridge = _require_bridge(bridge_id, family_id)
    if bridge.paired:
        raise HTTPException(status_code=409, detail="this bridge phone is already paired")
    code, expires_at = bridges_store.create_pair_code(bridge.id)
    return PairCodeOut(code=code, expiresAt=expires_at)


def _normalize(raw: str | None, field: str) -> str | None:
    return normalize_optional_phone(raw, field)


def _number_taken(owner_uid: str, number: str, bridge: bridges_store.Bridge) -> None:
    """409 when `number` already belongs to another member or another bridge."""
    detail = bridge_numbers.number_taken(owner_uid, number, bridge)
    if detail is not None:
        raise HTTPException(status_code=409, detail=detail)


@router.patch("/bridges/{bridge_id}", dependencies=[Depends(require_family_write_rate_limit)])
def patch_bridge(
    bridge_id: str,
    req: PatchBridgeRequest,
    scope: FamilyScope,
    broker: Annotated[BrokerClient, Depends(get_broker)],
) -> BridgeOut:
    """Label, reassign (`ownerUid`) and set accepted numbers (non-null sets, `""` clears).

    The owner's `smsNumber` follows the bridge (SIM, else Voice); a number held elsewhere is a 409.
    """
    _, family_id = scope
    bridge = _require_bridge(bridge_id, family_id)
    fields = req.model_fields_set

    new_sim = (
        _normalize(req.simNumber, "simNumber") if "simNumber" in fields else bridge.simNumber
    )
    new_voice = (
        _normalize(req.voiceNumber, "voiceNumber") if "voiceNumber" in fields else bridge.voiceNumber
    )
    new_owner_uid = req.ownerUid if req.ownerUid is not None else bridge.ownerUid
    new_owner = _require_active_person(new_owner_uid, family_id)
    owner_changed = new_owner_uid != bridge.ownerUid
    numbers_changed = (new_sim, new_voice) != (bridge.simNumber, bridge.voiceNumber)
    if not owner_changed and not numbers_changed:
        if req.label is not None:
            bridges_store.set_label(bridge.id, req.label.strip())
        fresh = bridges_store.get(bridge.id)
        assert fresh is not None
        return _bridge_out(fresh)

    for number in (new_sim, new_voice):
        if number is not None:
            _number_taken(new_owner_uid, number, bridge)

    if req.label is not None:
        bridges_store.set_label(bridge.id, req.label.strip())
    unsubscribed = 0
    if owner_changed:
        # the old owner's subscriptions, queued texts and channels go; runs before ownerUid changes
        principal, _ = scope
        unsubscribed = chat_subscribe.reassign_conversations(
            family_id, bridge, new_owner_uid, principal.uid, broker
        )
        for item in bridge_outbox.fail_pending(bridge.id, "reassigned"):
            if item.msgId and item.bid:
                deliveries.mark_failed(item.msgId, item.bid, error="reassigned")
    if bridge.paired:
        # release the previous owner's or numbers first
        bridge_numbers.release_numbers(bridge, broker)
    caps = bridge_numbers.caps_for(
        bridge.status.smsCapable or bridge.caps.sms,
        bridge.caps.gchat,
        new_sim,
        new_voice,
        whatsapp=bridge.caps.whatsapp,
    )
    if owner_changed:
        bridges_store.set_owner(bridge.id, new_owner_uid)
    bridges_store.set_numbers(bridge.id, sim_number=new_sim, voice_number=new_voice, caps=caps)
    bridges_store.set_error(bridge.id, None)
    fresh = bridges_store.get(bridge.id)
    assert fresh is not None
    if fresh.paired:
        bridge_numbers.apply_numbers(fresh, broker)
    if owner_changed:
        bridge_numbers.rederive(new_owner.uid, broker)
    fresh = bridges_store.get(bridge.id)
    assert fresh is not None
    out = _bridge_out(fresh)
    out.unsubscribed = unsubscribed
    return out


@router.post(
    "/bridges/{bridge_id}/accept-sim", dependencies=[Depends(require_family_write_rate_limit)]
)
def accept_sim(
    bridge_id: str, scope: FamilyScope, broker: Annotated[BrokerClient, Depends(get_broker)]
) -> BridgeOut:
    """The SIM number the phone reports becomes the accepted one; clears `status.error`."""
    _, family_id = scope
    bridge = _require_bridge(bridge_id, family_id)
    reported = bridge.status.simNumber
    if not bridge.paired:
        raise HTTPException(status_code=409, detail="the bridge phone is not paired")
    if reported is None:
        raise HTTPException(status_code=409, detail="the phone has not reported a SIM number")
    _require_active_person(bridge.ownerUid, family_id)
    if reported != bridge.simNumber:
        _number_taken(bridge.ownerUid, reported, bridge)
        bridge_numbers.release_numbers(bridge, broker)
        caps = bridge_numbers.caps_for(
            bridge.status.smsCapable or bridge.caps.sms,
            bridge.caps.gchat,
            reported,
            bridge.voiceNumber,
            whatsapp=bridge.caps.whatsapp,
        )
        bridges_store.set_numbers(
            bridge.id, sim_number=reported, voice_number=bridge.voiceNumber, caps=caps
        )
    bridges_store.set_error(bridge.id, None)
    fresh = bridges_store.get(bridge.id)
    assert fresh is not None
    bridge_numbers.apply_numbers(fresh, broker)
    fresh = bridges_store.get(bridge.id)
    assert fresh is not None
    return _bridge_out(fresh)


@router.delete("/bridges/{bridge_id}", dependencies=[Depends(require_family_write_rate_limit)])
def unpair_bridge(
    bridge_id: str, scope: FamilyScope, broker: Annotated[BrokerClient, Depends(get_broker)]
) -> BridgeOut:
    """Clear the tokens and the owner's `smsNumber` if a bridge number; fail pending outbox rows."""
    _, family_id = scope
    bridge = _require_bridge(bridge_id, family_id)
    bridges_store.unpair(bridge.id)
    bridge_health.forget(bridge)
    for item in bridge_outbox.fail_pending(bridge.id, "unpaired"):
        if item.msgId and item.bid:
            deliveries.mark_failed(item.msgId, item.bid, error="unpaired")
    bridge_numbers.release_numbers(bridge, broker)
    fresh = bridges_store.get(bridge.id)
    assert fresh is not None
    logger.info("bridge unpaired bridge=%s owner=%s", bridge.id, bridge.ownerUid)
    return _bridge_out(fresh)
