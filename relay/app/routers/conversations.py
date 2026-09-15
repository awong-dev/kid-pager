"""`/api/conversations/*`: every message write, group join/leave and `/locate`."""

from __future__ import annotations

import time
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request

from app.location import Location, NoLocatableDevice
from app.routing import Routing
from app.store import allow as allow_store
from app.store import devices as devices_store
from app.store import messages as messages_store
from app.store import users as users_store
from app.wire import (
    BODY_MAX_UTF8_BYTES,
    strip_control_chars,
)

router = APIRouter(prefix="/api/conversations")


class SendMessageRequest(BaseModel):
    body: str


class SendMessageResponse(BaseModel):
    id: str


def get_routing(request: Request) -> Routing:
    return request.app.state.routing


def get_location(request: Request) -> Location:
    return request.app.state.location


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
    if len(body.encode("utf-8")) > BODY_MAX_UTF8_BYTES:

    result = routing.send(
        sender_uid=authed.uid,
        kind="text",
        body=body,
        origin_backend_kind="webapp",
    )
    if result.rejected:
        reason = result.rejected[0].reason
        if reason == "unknown_alias":
            raise HTTPException(status_code=404, detail="unknown recipient")
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

    bid = messages_store.find_delivery_by_kind(msg, "webapp")
    if bid is None:
        raise HTTPException(status_code=404, detail="no webapp delivery for this message")

    # Clear the reader's unread count in the same transaction as the ack.
    messages_store.apply_delivery_ack(
        msg.id, bid, None, "read", int(time.time()), clear_unread_uid=authed.uid
    )
    return {"ok": True}




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
