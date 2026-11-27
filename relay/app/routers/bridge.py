"""`/bridge/*`: the headless bridge phone API.

`POST /bridge/pair` is unauthenticated (the pair code is the credential); the rest need a bridge token.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
from starlette.concurrency import run_in_threadpool

from app import alerts as alerts_module
from app import (
    bridge_health,
    bridge_numbers,
    chat_subscribe,
    deliveries,
    link_health,
    move,
)
from app.bridgeauth import mint_token, require_bridge
from app.carriers import service as carriers_service
from app.carriers.base import SimInfo
from app.db import sqlitestore
from app.db.firestore import get_db
from app.inbound_text import handle_text, placeholder_for
from app.routers.webhooks import _check_webhook_ip_rate_limit, _client_ip
from app.sms_text import redact_phone, voice_link
from app.store import bridge_conversations, bridge_outbox
from app.store import bridges as bridges_store
from app.store import externals as externals_store
from app.store import families as families_store
from app.store import held_chat as held_chat_store
from app.store import rate_limits as rate_limits_store
from app.store import users as users_store

logger = logging.getLogger("relay.bridge")

router = APIRouter(prefix="/bridge")

PAIR_GLOBAL_LIMIT = 10
PAIR_GLOBAL_WINDOW_S = 60


class PairCaps(BaseModel):
    model_config = ConfigDict(extra="ignore")

    sms: bool = False
    gchat: bool = False
    gvoice: bool = False
    whatsapp: bool = False


class PairRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    code: str = Field(max_length=32)
    version: str | None = Field(default=None, max_length=64)
    accounts: list[Annotated[str, Field(max_length=200)]] = Field(default_factory=list, max_length=10)
    simNumber: str | None = Field(default=None, max_length=32)
    voiceNumber: str | None = Field(default=None, max_length=32)
    caps: PairCaps = Field(default_factory=PairCaps)


class PairResponse(BaseModel):
    bridgeId: str
    token: str
    outboxWaitS: int | None = None  # omitted when 0


def normalize_optional_phone(raw: str | None, field: str) -> str | None:
    if raw is None or not raw.strip():
        return None
    try:
        return externals_store.normalize_phone(raw)
    except ValueError:
        raise HTTPException(status_code=422, detail=f"{field} is not a valid phone number") from None


@router.post("/pair", response_model_exclude_none=True)
def pair(req: PairRequest, request: Request) -> PairResponse:
    """Consume the code, mint the token, store the accepted numbers and set `smsNumber`."""
    _check_webhook_ip_rate_limit(request, "bridge")
    if not rate_limits_store.check_and_increment(
        "bridge_pair:global", limit=PAIR_GLOBAL_LIMIT, window_s=PAIR_GLOBAL_WINDOW_S
    ):
        raise HTTPException(status_code=429, detail="too many requests")
    sim = normalize_optional_phone(req.simNumber, "simNumber")
    voice = normalize_optional_phone(req.voiceNumber, "voiceNumber")

    # Numbers are unique across the relay: check before consuming the code, so a 409 keeps it.
    peeked_id = bridges_store.peek_pair_code(req.code)
    peeked = bridges_store.get(peeked_id) if peeked_id else None
    if peeked is not None and not peeked.paired:
        for number in (sim, voice):
            if number is not None:
                detail = bridge_numbers.number_taken(peeked.ownerUid, number, peeked)
                if detail is not None:
                    raise HTTPException(status_code=409, detail=detail)

    bridge_id = bridges_store.consume_pair_code(req.code)
    bridge = bridges_store.get(bridge_id) if bridge_id else None
    if bridge is None or bridge.paired:
        raise HTTPException(status_code=404, detail="unknown or expired code")

    token, token_hash = mint_token(bridge.id)
    caps = bridge_numbers.caps_for(
        req.caps.sms, req.caps.gchat, sim, voice, whatsapp=req.caps.whatsapp
    )
    status = {
        "accounts": req.accounts,
        "smsCapable": req.caps.sms,
        "whatsapp": req.caps.whatsapp,
        "simNumber": sim,
        "voiceNumber": voice,
        "version": req.version,
    }
    bridges_store.set_token_hash(bridge.id, token_hash, caps=caps, status=status)
    bridges_store.set_numbers(bridge.id, sim_number=sim, voice_number=voice, caps=caps)
    fresh = bridges_store.get(bridge.id)
    assert fresh is not None
    bridge_numbers.apply_numbers(fresh, request.app.state.broker)
    logger.info("bridge paired bridge=%s owner=%s sim=%s voice=%s", bridge.id, bridge.ownerUid,
                bool(sim), bool(voice))
    wait_s = request.app.state.settings.bridge_outbox_wait_s
    return PairResponse(bridgeId=bridge.id, token=token, outboxWaitS=wait_s or None)


def bridge_moved(
    bridge: Annotated[bridges_store.Bridge, Depends(require_bridge)],
) -> dict[str, str] | None:
    """The `moved` object while this relay sends bridge phones elsewhere (docs/MOVE_DESIGN.md §8.3)."""
    return move.bridge_moved()


def _refused_moved(moved: dict[str, str]) -> JSONResponse:
    return JSONResponse(status_code=503, content={"detail": "moved", "moved": moved})


HELLO_LIMIT = 20
HELLO_WINDOW_S = 60
HELLO_BRIDGE_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
HELLO_NONCE_RE = re.compile(r"^[0-9a-f]{32}$")


@router.get("/hello")
def hello(request: Request, bridge: str = "", nonce: str = "") -> JSONResponse:
    """Proves this relay holds the bridge's token hash and this move id, without the token (§8.4)."""
    if HELLO_BRIDGE_RE.fullmatch(bridge) is None or HELLO_NONCE_RE.fullmatch(nonce) is None:
        return JSONResponse(status_code=400, content={"detail": "bad request"})
    if not rate_limits_store.check_and_increment(
        f"bridge-hello:{_client_ip(request)}", limit=HELLO_LIMIT, window_s=HELLO_WINDOW_S
    ):
        return JSONResponse(status_code=429, content={"detail": "too many requests"})
    rec = move.get_record()
    row = bridges_store.get(bridge)
    if rec is None or row is None or row.tokenHash is None:
        return JSONResponse(status_code=404, content={"detail": "unavailable"})
    return JSONResponse(
        {
            "bridgeId": row.id,
            "moveId": rec.id,
            "sig": move.hello_sig(row.tokenHash, nonce, row.id, rec.id),
        }
    )


class HeartbeatStatus(BaseModel):
    """What the phone may report; typed so a bad value is a 422. `error` is relay-owned, ignored."""

    model_config = ConfigDict(extra="ignore")

    battery: int | None = Field(default=None, ge=0, le=100)
    listenerBound: bool | None = None
    smsDefault: bool | None = None
    accessibility: bool | None = None
    whatsapp: bool | None = None
    accounts: list[Annotated[str, Field(max_length=120)]] | None = Field(default=None, max_length=10)
    simNumber: str | None = Field(default=None, max_length=32)
    voiceNumber: str | None = Field(default=None, max_length=32)
    version: str | None = Field(default=None, max_length=40)
    error: str | None = Field(default=None, max_length=200)
    charging: bool | None = None
    batteryExempt: bool | None = None
    carrier: SimInfo | None = None


class HeartbeatRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    status: HeartbeatStatus = Field(default_factory=HeartbeatStatus)
    fcmToken: str | None = Field(default=None, max_length=4096)


SIM_CHANGED = "SIM changed"


@router.post("/heartbeat")
def heartbeat(
    req: HeartbeatRequest,
    request: Request,
    bridge: Annotated[bridges_store.Bridge, Depends(require_bridge)],
    moved: Annotated[dict[str, str] | None, Depends(bridge_moved)],
) -> dict:
    """Record the status; a reported SIM differing from the accepted one sets `status.error`."""
    if moved is not None:
        return {"pending": 0, "moved": moved}
    status = req.status.model_dump(exclude_unset=True, exclude={"error"})
    if status.get("smsDefault") is not None:
        status["smsCapable"] = status["smsDefault"]
    for key in ("simNumber", "voiceNumber"):
        value = status.get(key)
        if isinstance(value, str) and value.strip():
            try:
                status[key] = externals_store.normalize_phone(value)
            except ValueError:
                status[key] = None
        elif key in status:
            status[key] = None
    for key in ("charging", "batteryExempt", "carrier"):
        if status.get(key) is None:
            status.pop(key, None)  # absent = unknown: keep what is stored
    bridges_store.touch(bridge.id, status, req.fcmToken)
    if status.get("whatsapp") is None:
        status.pop("whatsapp", None)
    if status.get("listenerBound") is None:
        status.pop("listenerBound", None)
    if "smsCapable" in status or "whatsapp" in status or "listenerBound" in status:
        caps = bridge_numbers.caps_for(
            status.get("smsCapable", bridge.status.smsCapable or bridge.caps.sms),
            status.get("listenerBound", bridge.caps.gchat),
            bridge.simNumber,
            bridge.voiceNumber,
            whatsapp=status.get("whatsapp", bridge.caps.whatsapp),
        )
        if caps != bridge.caps:
            bridges_store.set_caps(bridge.id, caps)
    reported = status.get("simNumber")
    if reported is not None and reported != bridge.simNumber:
        bridges_store.set_error(bridge.id, SIM_CHANGED)
    elif bridge.status.error == SIM_CHANGED:
        bridges_store.set_error(bridge.id, None)
    _after_heartbeat(bridge.id, request)
    out: dict[str, int] = {"pending": bridge_outbox.count_pending(bridge.id)}
    wait_s = request.app.state.settings.bridge_outbox_wait_s
    if wait_s > 0:
        out["outboxWaitS"] = wait_s
    return out


def _after_heartbeat(bridge_id: str, request: Request) -> None:
    """Evaluate bridge health too, so "restored" is immediate. Never fails the heartbeat."""
    try:
        fresh = bridges_store.get(bridge_id)
        if fresh is not None:
            carriers_service.on_heartbeat(fresh)
            bridge_health.evaluate(fresh, heartbeat=True)
    except Exception:
        logger.exception("bridge health after heartbeat failed bridge=%s", bridge_id)


OUTBOX_WAIT_CAP_S = 25
OUTBOX_POLL_INTERVAL_S = 2


@router.get("/outbox")
async def get_outbox(
    bridge: Annotated[bridges_store.Bridge, Depends(require_bridge)],
    moved: Annotated[dict[str, str] | None, Depends(bridge_moved)],
    wait: Annotated[int, Query(ge=0)] = 0,
) -> dict:
    """Pending items, oldest first, at most 20; `wait` (cap 25 s) long-polls without a thread."""
    if moved is not None:
        return {"items": [], "moved": moved}
    deadline = time.monotonic() + min(wait, OUTBOX_WAIT_CAP_S)
    woke = asyncio.Event()
    unsubscribe = None
    db = get_db()
    if wait > 0 and isinstance(db, sqlitestore.Client):
        loop = asyncio.get_running_loop()
        outbox_parent = f"bridges/{bridge.id}/outbox"

        def _on_change(path: str, collection: str, parent: str, data: dict | None) -> None:
            if parent == outbox_parent:
                loop.call_soon_threadsafe(woke.set)

        unsubscribe = db.subscribe(_on_change)
    try:
        while True:
            woke.clear()
            items = await run_in_threadpool(bridge_outbox.list_pending, bridge.id)
            remaining = deadline - time.monotonic()
            if items or remaining <= 0:
                return {"items": [i.for_phone() for i in items]}
            if unsubscribe is None:
                await asyncio.sleep(min(OUTBOX_POLL_INTERVAL_S, remaining))
            else:
                try:
                    await asyncio.wait_for(woke.wait(), remaining)
                except TimeoutError:
                    pass
    finally:
        if unsubscribe is not None:
            unsubscribe()


class AckRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    state: Literal["sent", "failed"]
    reason: str | None = Field(default=None, max_length=200)
    tier: Literal[1, 2] = 1
    # SMS parts the phone sent (only with `sent`); lenient so a bad value never fails an ack.
    parts: int | None = None


def apply_ack_to_delivery(item: bridge_outbox.OutboxItem) -> None:
    """Re-apply the delivery transition from the stored row, so a retried ack repairs a lost write."""
    if not item.msgId or not item.bid:
        return
    if item.state == "sent":
        deliveries.mark_sent(item.msgId, item.bid)
    elif item.state == "failed":
        deliveries.mark_failed(item.msgId, item.bid, error=item.reason)


@router.post("/outbox/{ob_id}/ack", status_code=204)
def ack_outbox(
    ob_id: str,
    req: AckRequest,
    bridge: Annotated[bridges_store.Bridge, Depends(require_bridge)],
    moved: Annotated[dict[str, str] | None, Depends(bridge_moved)],
) -> Response:
    if moved is not None:
        return _refused_moved(moved)
    acked = bridge_outbox.ack(bridge.id, ob_id, req.state, req.reason, req.tier)
    if acked is None:
        raise HTTPException(status_code=404, detail="no such outbox item")
    item, transitioned = acked
    apply_ack_to_delivery(item)
    if transitioned:
        link_health.on_ack(bridge, item)
        carriers_service.on_ack(bridge, item, req.parts if req.state == "sent" else None)
    if transitioned and req.tier == 2:
        bridges_store.increment_tier2(bridge.id)
    latency_ms = -1
    if item.createdAt is not None and item.ackedAt is not None:
        latency_ms = int((item.ackedAt - item.createdAt).total_seconds() * 1000)
    logger.info(
        "bridge out bridge=%s ob=%s state=%s tier=%d ms=%d",
        bridge.id, ob_id, item.state, req.tier, latency_ms,
    )
    return Response(status_code=204)


EVENT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
EVENTS_PER_MINUTE = 120
MAX_EVENTS_PER_BATCH = 50


class EventConversation(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str = Field(min_length=1)
    title: str | None = Field(default=None, max_length=200)
    isGroup: bool = False
    link: str | None = Field(default=None, max_length=2048)

    @field_validator("id")
    @classmethod
    def _id_bytes(cls, v: str) -> str:
        if len(v.encode("utf-8")) > 512:
            raise ValueError("conversation.id longer than 512 bytes")
        return v


class EventSender(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str = Field(default="", max_length=200)
    phone: str | None = Field(default=None, max_length=64)


class EventAttachment(BaseModel):
    model_config = ConfigDict(extra="ignore")

    kind: Literal["image", "video", "audio", "file"]


class BridgeEvent(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    source: Literal["sms", "gchat", "gvoice", "whatsapp"]
    kind: Literal["message", "inspect"] = "message"
    conversation: EventConversation
    sender: EventSender = Field(default_factory=EventSender)
    # A sanity bound, not a message limit (the relay splits for the pager).
    text: str = Field(default="", max_length=8192)
    ts: float = 0
    attachments: list[EventAttachment] = Field(default_factory=list, max_length=20)
    people: list[Annotated[str, Field(max_length=200)]] = Field(default_factory=list, max_length=64)

    @field_validator("id")
    @classmethod
    def _id_shape(cls, v: str) -> str:
        if not EVENT_ID_RE.match(v):
            raise ValueError("event id must match ^[A-Za-z0-9_-]{1,64}$")
        return v


class EventsRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    events: list[BridgeEvent] = Field(max_length=MAX_EVENTS_PER_BATCH)


def conv_log_id(conversation_id: str) -> str:
    return hashlib.sha256(conversation_id.encode()).hexdigest()[:8]


def _from_label(event: BridgeEvent) -> str:
    if event.sender.phone:
        return redact_phone(event.sender.phone)
    return event.sender.name or "-"


def _handle_text_event(
    bridge: bridges_store.Bridge, event: BridgeEvent, routing
) -> str:
    """`sms` / `gvoice` messages: the target is always the bridge owner."""
    if event.kind != "message":
        return "dropped_unsupported"
    if event.conversation.isGroup:
        return "dropped_group"
    target = users_store.get_user(bridge.ownerUid)
    if (
        target is None
        or target.kind != "person"
        or target.disabled
        or target.familyId is None
    ):
        return "dropped_owner"
    caps_ok = {
        "sms": bridge.caps.sms,
        "gvoice": bridge.caps.gvoice,
    }.get(event.source, False)
    if not caps_ok:
        return "dropped_cap"
    try:
        from_number = externals_store.normalize_phone(event.sender.phone or "")
    except ValueError:
        return "dropped_bad_from"
    if event.source == "sms" and carriers_service.is_service_number(bridge, from_number):
        # The carrier's own texts are never a message.
        return carriers_service.handle_inbound(bridge, from_number, event.text, event.id)
    sid = f"br_{bridge.id}_{event.id}"
    # Voice carries a thread id and a deep link for the tier-2 reply.
    conv_link = voice_link if event.source == "gvoice" else None

    def reply(text: str) -> None:
        bridge_outbox.enqueue_hint(
            bridge,
            source=event.source,
            phone=from_number,
            text=text,
            wire_id=sid,
            to_extra=(
                {"conversationId": event.conversation.id, "link": conv_link(from_number)}
                if conv_link
                else None
            ),
        )

    return handle_text(
        target,
        from_number,
        event.text,
        sid,
        routing,
        reply=reply,
        attachments=[a.kind for a in event.attachments],
        via=event.source,
        voice_conv=event.conversation.id if conv_link else None,
    )


CONVERSATIONS_PER_DAY = 50

# WhatsApp DM is `<digits>@lid` or `<digits>@s.whatsapp.net`, a group `<digits-and-dashes>@g.us`;
# anything else changes per notification.
WA_CONV_RE = re.compile(r"^(\d+@(lid|s\.whatsapp\.net)|[\d-]+@g\.us)$")


def _preview(event: BridgeEvent) -> str:
    return (event.text.strip() or (placeholder_for([a.kind for a in event.attachments]) if event.attachments else ""))


def _handle_chat_event(
    bridge: bridges_store.Bridge, event: BridgeEvent, routing, broker
) -> str:
    """`gchat` events."""
    conv = event.conversation
    if event.kind == "inspect":
        # The answer to a health probe is not "add by link".
        probed = link_health.apply_probe_result(bridge, conv.link, conv.title)
        if probed is not None:
            return probed
    existing = bridge_conversations.get(bridge.id, conv.id)
    target = users_store.get_user(bridge.ownerUid)
    if target is None or target.kind != "person" or target.disabled or target.familyId is None:
        return "dropped_owner"
    caps_ok = {
        "gchat": bridge.caps.gchat,
        "gvoice": bridge.caps.gvoice,
        "whatsapp": bridge.caps.whatsapp,
    }.get(event.source, False)
    if not caps_ok:
        return "dropped_cap"
    if event.source == "whatsapp" and event.sender.name.startswith("~"):
        # unsaved group members show as "~ Name"
        event = event.model_copy(
            update={"sender": event.sender.model_copy(update={"name": event.sender.name.lstrip("~").strip()})}
        )
    title = conv.title
    if not conv.isGroup and not title:
        # a 1:1 row is titled by the peer's display name
        title = event.sender.name or None
    if existing is None:
        since = datetime.now(UTC) - timedelta(days=1)
        if bridge_conversations.count_created_since(bridge.id, since) >= CONVERSATIONS_PER_DAY:
            return "dropped_conv_cap"
    if event.kind == "inspect":
        bridge_conversations.upsert_seen(
            bridge,
            source=event.source,
            conversation_id=conv.id,
            title=title,
            is_group=conv.isGroup,
            link=conv.link,
            speaker=None,
            people=event.people,
            preview=None,
            inspected=True,
        )
        return "inspected"
    if existing is not None and existing.status == "ignored":
        return "dropped_ignored"
    if existing is not None and existing.status == "paused":
        return "dropped_paused"
    body = _preview(event)
    if not body:
        return "dropped_empty"
    row = bridge_conversations.upsert_seen(
        bridge,
        source=event.source,
        conversation_id=conv.id,
        title=title,
        is_group=conv.isGroup,
        link=conv.link,
        speaker=event.sender.name,
        people=event.people,
        preview=body,
    )
    if row.status == "subscribed":
        link_health.on_inbound(row)
        outcome = chat_subscribe.deliver_subscribed(
            bridge, row, event, routing, broker, existing.title if existing else None
        )
        if outcome is not None:
            return outcome
    hid = f"br_{bridge.id}_{event.id}"
    if held_chat_store.count_held(row.id) >= held_chat_store.HELD_CAP:
        return "held_cap"
    if not held_chat_store.create(
        hid,
        bridge_id=bridge.id,
        conversation_id=conv.id,
        conv_row_id=row.id,
        family_id=target.familyId,
        to_uid=target.uid,
        sender_name=event.sender.name,
        body=body,
    ):
        return "duplicate"
    alert_id = alerts_module.chat_held_upsert(
        target.familyId, target, row, body, event.sender.name or "someone"
    )
    bridge_conversations.set_fields(
        row.id, heldCount=held_chat_store.count_held(row.id), alertId=alert_id
    )
    return "held"


def process_event(bridge: bridges_store.Bridge, event: BridgeEvent, routing, broker) -> str:
    if families_store.is_frozen(bridge.familyId):
        # `error` keeps the event queued on the phone until the family is unfrozen.
        return "error"
    if event.source == "whatsapp":
        # every WhatsApp conversation is keyed by its conversation id and takes the chat path
        if not WA_CONV_RE.match(event.conversation.id):
            return "dropped_bad_conv"
        return _handle_chat_event(bridge, event, routing, broker)
    if event.source in ("sms", "gvoice") and event.kind == "message":
        return _handle_text_event(bridge, event, routing)
    if event.source in ("gchat", "gvoice"):
        return _handle_chat_event(bridge, event, routing, broker)
    return "dropped_unsupported"


def process_events(
    bridge: bridges_store.Bridge, events: list[BridgeEvent], routing, broker
) -> list[dict]:
    if events and not rate_limits_store.check_and_increment(
        f"bridge_events:{bridge.id}", limit=EVENTS_PER_MINUTE, window_s=60, count=len(events)
    ):
        raise HTTPException(status_code=429, detail="too many requests")
    results = []
    for event in events:
        try:
            outcome = process_event(bridge, event, routing, broker)
        except Exception:
            logger.exception(
                "bridge event failed bridge=%s src=%s conv=%s event=%s",
                bridge.id, event.source, conv_log_id(event.conversation.id), event.id,
            )
            outcome = "error"
        kind = ""
        if event.source == "whatsapp":
            cid = event.conversation.id
            kind = " kind=" + ("lid" if cid.endswith("@lid") else "grp" if cid.endswith("@g.us") else "jid")
        logger.info(
            "bridge in bridge=%s src=%s conv=%s from=%s%s outcome=%s",
            bridge.id, event.source, conv_log_id(event.conversation.id), _from_label(event), kind, outcome,
        )
        results.append({"id": event.id, "outcome": outcome})
    return results


@router.post("/events", response_model=None)
async def post_events(
    req: EventsRequest,
    request: Request,
    bridge: Annotated[bridges_store.Bridge, Depends(require_bridge)],
    moved: Annotated[dict[str, str] | None, Depends(bridge_moved)],
) -> dict | JSONResponse:
    if moved is not None:
        return _refused_moved(moved)
    results = await run_in_threadpool(
        process_events, bridge, req.events, request.app.state.routing, request.app.state.broker
    )
    return {"results": results}
