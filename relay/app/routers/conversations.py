"""`/api/conversations/*`: every message write, group join/leave and `/locate`."""

from __future__ import annotations

import time
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from app import book
from app.auth import AuthedUser, principal_for, require_user
from app.broker import BrokerClient
from app.location import Location, NoLocatableDevice
from app.routing import Routing
from app.store import allow as allow_store
from app.store import conversations as conversations_store
from app.store import devices as devices_store
from app.store import externals as externals_store
from app.store import messages as messages_store
from app.store import users as users_store
from app.store.conversations import Conversation
from app.wire import (
    BODY_MAX_UTF8_BYTES,
    strip_control_chars,
)

router = APIRouter(prefix="/api/conversations")


class SendMessageRequest(BaseModel):
    body: str


class SendMessageResponse(BaseModel):
    id: str


# Human text for a policy-gated 403's `{reason, message}` body; `policy_in` is formatted per call.
_POLICY_REJECT_MESSAGES = {
    "policy_out": "Your family admin has limited who you can message.",
    "not_allowed": "You are not on each other's approved lists.",
    "sms_contact": "SMS contacts can't text each other.",
    "no_sms_number": "You have no SMS number; ask your family admin.",
    "no_bridge": "Google Chat is not set up for this chat; ask your family admin.",
    "family_frozen": "That family is moving servers; try again later.",
    "no_peer": "That server is not connected; ask your family admin.",
    "fed_reauth": "That family was restored from a backup and must be re-linked; ask your family admin.",
    "fed_off": "Messaging other servers is turned off; ask your server admin.",
}


def get_routing(request: Request) -> Routing:
    return request.app.state.routing


def get_location(request: Request) -> Location:
    return request.app.state.location


def get_broker(request: Request) -> BrokerClient:
    """The app's broker client."""
    return request.app.state.broker


def _resolve_message_alias(alias: str, sender_uid: str) -> str:
    """Resolve a phone-shaped alias to the sender's family SMS contact, else return it unchanged."""
    if users_store.get_uid_for_alias(alias) is not None:
        return alias
    if conversations_store.get_by_alias(alias) is not None:
        return alias
    try:
        phone = externals_store.normalize_phone(alias)
    except ValueError:
        return alias
    sender = users_store.get_user(sender_uid)
    if sender is None or sender.familyId is None:
        return alias
    # the sender's family's own contact for this number
    existing = externals_store.get_family_contact(sender.familyId, phone)
    if existing is not None:
        return existing.alias
    return alias


@router.post("/{alias}/messages", status_code=201)
def send_message(
    alias: str,
    req: SendMessageRequest,
    authed: Annotated[AuthedUser, Depends(require_user)],
    routing: Annotated[Routing, Depends(get_routing)],
) -> SendMessageResponse:
    # strip control chars; reject if empty or oversize after stripping
    body = strip_control_chars(req.body)
    if not body:
        raise HTTPException(
            status_code=400, detail="body is empty after stripping control characters"
        )
    # one web message is one wire message (1024 bytes); a longer text is split by Routing
    if len(body.encode("utf-8")) > BODY_MAX_UTF8_BYTES:
        raise HTTPException(
            status_code=400, detail=f"body exceeds {BODY_MAX_UTF8_BYTES} UTF-8 bytes"
        )

    resolved_alias = _resolve_message_alias(alias, authed.uid)
    result = routing.send(
        sender_uid=authed.uid,
        recipient_alias=resolved_alias,
        kind="text",
        body=body,
        origin_backend_kind="webapp",
    )
    if result.rejected:
        reason = result.rejected[0].reason
        if reason == "unknown_alias":
            raise HTTPException(status_code=404, detail="unknown recipient")
        if reason == "not_member":
            raise HTTPException(status_code=403, detail="not allowed to message this recipient")
        if reason == "hook_blocked":
            # the web shows `notifySender`
            raise HTTPException(
                status_code=403,
                detail={
                    "reason": reason,
                    "message": result.notify or "This message was blocked by a filter.",
                },
            )
        # policy_out / policy_in / not_allowed: the web shows the server's `{reason, message}`.
        message = (
            f"@{alias} is not accepting messages from you."
            if reason == "policy_in"
            else _POLICY_REJECT_MESSAGES[reason]
        )
        raise HTTPException(status_code=403, detail={"reason": reason, "message": message})
    if not result.messages:
        # unreachable: no `wire_id` is passed, so dedup cannot fire; kept visible, not asserted
        raise HTTPException(status_code=500, detail="send failed")
    return SendMessageResponse(id=result.messages[0].id)


@router.post("/{alias}/messages/{msg_id}/read")
def mark_read(
    alias: str,
    msg_id: str,
    authed: Annotated[AuthedUser, Depends(require_user)],
) -> dict[str, bool]:
    msg = messages_store.get_message(msg_id)
    if msg is None or msg.recipientUid != authed.uid:
        # Same 404 for "missing" and "not yours" so a probe cannot tell them apart.
        raise HTTPException(status_code=404, detail="no such message")

    # `alias` must name this message's conversation (a user DM peer or a group), else 404.
    # Try the DM resolution first: a group alias has no `uid` field.
    peer_uid = users_store.get_uid_for_alias(alias)
    if peer_uid is not None:
        if msg.convKey != messages_store.conv_key(authed.uid, peer_uid):
            raise HTTPException(status_code=404, detail="no such message")
    else:
        group = conversations_store.get_by_alias(alias)
        if group is None or msg.convKey != group.convKey or authed.uid not in group.uids:
            raise HTTPException(status_code=404, detail="no such message")

    bid = messages_store.find_delivery_by_kind(msg, "webapp")
    if bid is None:
        raise HTTPException(status_code=404, detail="no webapp delivery for this message")

    # Clear the reader's unread count in the same transaction as the ack.
    messages_store.apply_delivery_ack(
        msg.id, bid, None, "read", int(time.time()), clear_unread_uid=authed.uid
    )
    return {"ok": True}


def _push_book_to_members(uids: list[str], broker: BrokerClient) -> None:
    """Bump and push each member's book (a uid with no device is a no-op)."""
    book.bump_and_push(uids, broker, reason="group")


def _create_missing_message_edges(member_uids: list[str]) -> None:
    """Create `message`-only `allow` edges both ways between every member pair, never `locate`.

    Existing edges are left alone so an admin's allow-list is never widened or narrowed.
    """
    for a in member_uids:
        for b in member_uids:
            if a == b:
                continue
            if allow_store.get_edge(a, b) is None:
                allow_store.set_edge(a, b, message=True, locate=False)


class AddMemberRequest(BaseModel):
    uid: str


def _group_creator_family_id(group: Conversation) -> str | None:
    creator = users_store.get_user(group.createdBy) if group.createdBy else None
    return creator.familyId if creator is not None else None


@router.post("/{alias}/members", status_code=200)
def add_group_member(
    alias: str,
    req: AddMemberRequest,
    authed: Annotated[AuthedUser, Depends(require_user)],
    broker: Annotated[BrokerClient, Depends(get_broker)],
) -> Conversation:
    group = conversations_store.get_by_alias(alias)
    if group is None:
        raise HTTPException(status_code=404, detail="no such group")
    if group.bridge is not None:
        raise HTTPException(status_code=409, detail="managed under Google Chat")

    # joining needs a family admin of the group creator's family (or super)
    principal = principal_for(authed)
    is_authorized = principal.role == "super" or (
        principal.role == "admin"
        and principal.family_id is not None
        and principal.family_id == _group_creator_family_id(group)
    )
    if not is_authorized:
        raise HTTPException(
            status_code=403,
            detail="only a family admin of the group creator's family may add members",
        )
    new_member = users_store.get_user(req.uid)
    if new_member is None:
        raise HTTPException(status_code=404, detail=f"no such user: {req.uid!r}")
    if new_member.kind == "external":
        raise HTTPException(status_code=400, detail="an SMS contact cannot join a group")

    updated = conversations_store.add_member(group.convKey, req.uid)
    # `message` edges only, never `locate`
    _create_missing_message_edges(updated.uids)
    # push to every current member; harmless for the existing ones
    _push_book_to_members(updated.uids, broker)
    return updated


@router.delete("/{alias}/members/me", status_code=200)
def leave_group(
    alias: str,
    authed: Annotated[AuthedUser, Depends(require_user)],
    broker: Annotated[BrokerClient, Depends(get_broker)],
) -> Conversation:
    group = conversations_store.get_by_alias(alias)
    if group is None:
        raise HTTPException(status_code=404, detail="no such group")
    if authed.uid not in group.uids:
        raise HTTPException(status_code=404, detail="not a member of this group")
    if group.bridge is not None:
        raise HTTPException(status_code=409, detail="managed under Google Chat")
    updated = conversations_store.remove_member(group.convKey, authed.uid)
    # the leaver is no longer in `updated.uids`, so push to them explicitly
    _push_book_to_members([authed.uid], broker)
    return updated


class LocateFixOut(BaseModel):
    lat: float
    lon: float
    accM: int | None = None
    fixTs: int
    src: str = "gnss"


class LocateResponse(BaseModel):
    # The in-flight `loc_req` message id; None on the cached path (PROTOCOL.md §13.3).
    requestId: str | None = None
    cached: bool = False
    fix: LocateFixOut | None = None


@router.post("/{alias}/locate", status_code=202)
def locate(
    alias: str,
    authed: Annotated[AuthedUser, Depends(require_user)],
    location: Annotated[Location, Depends(get_location)],
) -> LocateResponse:
    target_uid = users_store.get_uid_for_alias(alias)
    if target_uid is None:
        raise HTTPException(status_code=404, detail="unknown recipient")

    # A user may always locate their own device without a `locate` edge, as may a family admin of
    # the target's family. Super gets no bypass. The §13.3 rules 5-7 still apply.
    if target_uid != authed.uid:
        principal = principal_for(authed)
        target_user = users_store.get_user(target_uid)
        is_family_admin = (
            principal.role == "admin"
            and principal.family_id is not None
            and target_user is not None
            and target_user.familyId == principal.family_id
        )
        if not is_family_admin:
            edge = allow_store.get_edge(authed.uid, target_uid)
            if edge is None or not edge.locate:
                raise HTTPException(status_code=403, detail="not allowed to locate this user")

    # Multi-device case: pick the lowest non-revoked device id.
    candidates = [
        d for d in devices_store.list_devices(owner_uid=target_uid) if d.revokedAt is None
    ]
    if not candidates:
        raise HTTPException(status_code=409, detail="no locatable device")
    device = min(candidates, key=lambda d: d.id)

    try:
        outcome = location.locate(requester_uid=authed.uid, device=device)
    except NoLocatableDevice:
        raise HTTPException(status_code=409, detail="no locatable device") from None

    fix_out = None
    if outcome.fix is not None:
        fix_out = LocateFixOut(
            lat=outcome.fix.lat,
            lon=outcome.fix.lon,
            accM=outcome.fix.accM,
            fixTs=outcome.fix.fixTs,
            src=outcome.fix.src,
        )
    return LocateResponse(requestId=outcome.request_id, cached=outcome.cached, fix=fix_out)
