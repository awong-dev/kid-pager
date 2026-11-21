"""Carrier balance bookkeeping: inbound replies, counting, checks and low/out thresholds."""

from __future__ import annotations

import logging
import time
from typing import Literal

from pydantic import BaseModel

from app import alerts as alerts_module
from app import notices, topups
from app.broker import BrokerClient
from app.carriers import registry, segments
from app.carriers.base import (
    Balance,
    BalanceDoc,
    CarrierState,
    Event,
    LowWarning,
    TopupFailed,
    TopupOk,
)
from app.ids import new_id
from app.store import bridge_outbox, carrier_log
from app.store import bridges as bridges_store
from app.store.bridges import Bridge

logger = logging.getLogger("relay.carriers")

LOW_AT = 20
CHECK_EVERY_PARTS = 25
CHECK_EVERY_S = 86400
CHECK_MIN_GAP_S = 3600
# An admin's "check now" is allowed this often (the automatic floor is an hour).
MANUAL_CHECK_MIN_GAP_S = 300
UNANSWERED_LIMIT = 3
CHECK_PREFIX = "ob_cr_bal_"
TOPUP_PREFIX = "ob_cr_tu_"


class CheckRefused(Exception):
    """A manual check could not be queued; `status` is the HTTP code and `detail` the text."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


def is_carrier_item(ob_id: str) -> bool:
    return ob_id.startswith(bridge_outbox.CARRIER_PREFIX)


def state_of(bridge: Bridge) -> CarrierState:
    return bridge.carrier or CarrierState()


def resolve(bridge: Bridge):
    return registry.get(state_of(bridge).id)


def left_of(state: CarrierState) -> int | None:
    """Texts left by the estimate; `None` when unknown or unlimited."""
    if state.balance is None or state.balance.texts is None:
        return None
    return state.balance.texts - state.sentSince


def estimate_only(state: CarrierState) -> bool:
    return state.awaiting and state.misses >= UNANSWERED_LIMIT - 1


class CarrierView(BaseModel):
    """`bridges/{id}.carrier` as the web renders it."""

    id: str
    name: str
    manual: bool
    # What the heartbeat's SIM info matched, whatever the admin chose.
    autoId: str
    canCheck: bool
    canTopup: bool
    # Texts left (clamped at 0), `None` when not known or unlimited.
    texts: int | None
    unlimited: bool
    # `carrier` / `manual` when nothing was sent since, else `estimate`; null when no
    # balance is known at all.
    source: Literal["carrier", "manual", "estimate"] | None
    balanceAt: int | None
    sentSince: int
    # ok / low / out
    level: str
    lastCheckAt: int | None
    unknownReplies: int
    # Three checks went unanswered: the number is an estimate and the raw replies are worth a look.
    estimateOnly: bool


def view(bridge: Bridge) -> CarrierView:
    state = state_of(bridge)
    carrier = registry.get(state.id)
    left = left_of(state)
    source: Literal["carrier", "manual", "estimate"] | None = None
    if state.balance is not None:
        stale = state.sentSince > 0 or estimate_only(state)
        source = "estimate" if stale else state.balance.source
    return CarrierView(
        id=carrier.id,
        name=carrier.name,
        manual=state.manual,
        autoId=registry.match(bridge.status.carrier).id,
        canCheck=carrier.balance_command() is not None,
        canTopup=bool(carrier.topup_packs()),
        texts=max(left, 0) if left is not None else None,
        unlimited=state.balance is not None and state.balance.texts is None,
        source=source,
        balanceAt=state.balance.at if state.balance else None,
        sentSince=state.sentSince,
        level=state.level,
        lastCheckAt=state.lastCheckAt,
        unknownReplies=state.unknownReplies,
        estimateOnly=estimate_only(state),
    )


def on_heartbeat(bridge: Bridge) -> None:
    """Auto-match the carrier from the SIM info unless an admin chose."""
    state = state_of(bridge)
    if state.manual or bridge.status.carrier is None:
        return
    matched = registry.match(bridge.status.carrier).id
    if matched == state.id and bridge.carrier is not None:
        return
    bridges_store.update_carrier(bridge.id, lambda s: s.model_copy(update={"id": matched}))


def set_carrier(bridge: Bridge, carrier_id: str | None) -> CarrierState:
    """The admin's override; `None` goes back to auto-matching."""
    if carrier_id is None:
        auto = registry.match(bridge.status.carrier).id
        update = {"manual": False, "id": auto}
    else:
        update = {"manual": True, "id": carrier_id}
    update.update({"awaiting": False, "misses": 0, "unknownReplies": 0})
    return bridges_store.update_carrier(bridge.id, lambda s: s.model_copy(update=update))


def set_balance(
    bridge: Bridge,
    texts: int | None,
    *,
    now: int | None = None,
    broker: BrokerClient | None = None,
) -> CarrierState:
    """The admin typed the allowance (`None`: unlimited); sent texts are subtracted from it."""
    now_s = int(now if now is not None else time.time())
    state = bridges_store.update_carrier(
        bridge.id,
        lambda s: s.model_copy(
            update={
                "balance": BalanceDoc(texts=texts, at=now_s, source="manual"),
                "sentSince": 0,
                "partsSinceCheck": 0,
            }
        ),
    )
    evaluate_level(bridge.id, now=now_s, broker=broker)
    return state


def is_service_number(bridge: Bridge, phone: str) -> bool:
    """True when `phone` (already normalized) is one of the bridge carrier's own numbers."""
    return phone in resolve(bridge).service_numbers


def handle_inbound(
    bridge: Bridge,
    sender: str,
    text: str,
    event_id: str,
    *,
    now: int | None = None,
    broker: BrokerClient | None = None,
) -> str:
    """The reply path: the raw text goes to `carrierLog` and the parsed event is applied."""
    now_s = int(now if now is not None else time.time())
    event = resolve(bridge).parse(sender, text)
    created = carrier_log.append(
        bridge.id, f"in_{event_id}", dir="in", party=sender, text=text, kind=event.kind,
        detail=_detail(event),
    )
    if not created:
        return "carrier_duplicate"
    if isinstance(event, Balance):
        _apply_balance(bridge, event.texts, now_s, broker)
    elif isinstance(event, LowWarning):
        if event.texts is not None:
            _apply_balance(bridge, event.texts, now_s, broker)
        else:
            _mark_check_due(bridge.id)
    elif isinstance(event, (TopupOk, TopupFailed)):
        on_topup_result(bridge, event, now_s, broker)
    else:
        bridges_store.update_carrier(
            bridge.id, lambda s: s.model_copy(update={"unknownReplies": s.unknownReplies + 1})
        )
    return f"carrier_{event.kind}"


def _detail(event: Event) -> str | None:
    data = event.model_dump(exclude={"kind"}, exclude_none=True)
    return ", ".join(f"{k}={v}" for k, v in data.items()) or None


def _mark_check_due(bridge_id: str) -> None:
    bridges_store.update_carrier(bridge_id, lambda s: s.model_copy(update={"checkDue": True}))


def _apply_balance(bridge: Bridge, texts: int | None, now: int, broker: BrokerClient | None) -> None:
    bridges_store.update_carrier(
        bridge.id,
        lambda s: s.model_copy(
            update={
                "balance": BalanceDoc(texts=texts, at=now, source="carrier"),
                "sentSince": 0,
                "partsSinceCheck": 0,
                "awaiting": False,
                "misses": 0,
                "checkDue": False,
            }
        ),
    )
    evaluate_level(bridge.id, now=now, broker=broker)


def on_topup_result(
    bridge: Bridge, event: TopupOk | TopupFailed, now: int, broker: BrokerClient | None
) -> None:
    """A top-up reply: on `TopupOk` the allowance goes up and a balance check follows."""
    topups.on_reply(bridge, event, now)
    if isinstance(event, TopupFailed):
        return
    carrier = resolve(bridge)
    packs = carrier.topup_packs()
    added = event.added if event.added is not None else (packs[0].texts if packs else 0)

    def fn(s: CarrierState) -> CarrierState:
        if event.balance is not None:
            bal = BalanceDoc(texts=event.balance, at=now, source="carrier")
            return s.model_copy(
                update={"balance": bal, "sentSince": 0, "partsSinceCheck": 0, "checkDue": True}
            )
        base = left_of(s) if s.balance is not None and s.balance.texts is not None else 0
        bal = BalanceDoc(texts=max(base or 0, 0) + added, at=now, source="estimate")
        return s.model_copy(
            update={"balance": bal, "sentSince": 0, "partsSinceCheck": 0, "checkDue": True}
        )

    state = bridges_store.update_carrier(bridge.id, fn)
    left = left_of(state)
    text = (
        f"Bridge phone texts topped up. {left} left."
        if left is not None
        else "Bridge phone texts topped up."
    )
    # `resolves` closes the open low alert and writes this one as a handled history row;
    # the out alert is closed by hand (one code per call).
    notices.notify(
        bridge.familyId, "sms_topped_up", text, key=bridge.id, resolves="sms_low", since=now,
        broker=broker,
    )
    alerts_module.resolve_system(bridge.familyId, "sms_out", bridge.id)
    evaluate_level(bridge.id, now=now, broker=broker)


def on_ack(
    bridge: Bridge,
    item: bridge_outbox.OutboxItem,
    parts: int | None,
    *,
    now: int | None = None,
    broker: BrokerClient | None = None,
) -> None:
    """Called once per outbox transition. Never raises into the ack."""
    now_s = int(now if now is not None else time.time())
    try:
        if is_carrier_item(item.id):
            _carrier_item_acked(bridge, item, now_s)
            return
        if item.kind != "send" or item.source != "sms":
            return
        if item.state == "sent":
            n = min(parts, 100) if parts and parts > 0 else segments.count_segments(item.text or "")
            bridges_store.update_carrier(
                bridge.id,
                lambda s: s.model_copy(
                    update={
                        "sentSince": s.sentSince + n,
                        "partsSinceCheck": s.partsSinceCheck + n,
                    }
                ),
            )
            evaluate_level(bridge.id, now=now_s, broker=broker)
        elif item.state == "failed" and (item.reason or "").startswith("sms_"):
            _mark_check_due(bridge.id)
    except Exception:
        logger.exception("carrier ack hook failed bridge=%s ob=%s", bridge.id, item.id)


def _carrier_item_acked(bridge: Bridge, item: bridge_outbox.OutboxItem, now: int) -> None:
    outcome = "sent" if item.state == "sent" else f"failed: {item.reason or '?'}"
    carrier_log.append(
        bridge.id, f"ack_{item.id}", dir="out", party=(item.to or {}).get("phone", ""),
        text=item.text or "", kind="ack", detail=outcome,
    )
    if item.id.startswith(TOPUP_PREFIX) and item.state == "failed":
        topups.on_command_failed(bridge, item.id[len(TOPUP_PREFIX):], item.reason)


def _level_for(left: int | None) -> str:
    if left is None or left > LOW_AT:
        return "ok"
    return "low" if left > 0 else "out"


def low_text(left: int) -> str:
    return f"Bridge phone has {left} text{'' if left == 1 else 's'} left."


OUT_TEXT = "Bridge phone is out of texts. SMS pages will not arrive."


def evaluate_level(
    bridge_id: str, *, now: int | None = None, broker: BrokerClient | None = None
) -> str | None:
    """Recompute ok / low / out and announce the edge; returns the level, `None` if no bridge."""
    bridge = bridges_store.get(bridge_id)
    if bridge is None:
        return None
    now_s = int(now if now is not None else time.time())
    state = state_of(bridge)
    left = left_of(state)
    target = _level_for(left)
    if target == state.level:
        return target
    track = notices.Health(
        state=state.level, since=state.levelSince, notifiedAt=state.levelNotifiedAt
    )
    notified = state.levelNotifiedAt
    if target == "ok":
        track, _ = notices.enter_ok(track, now_s)
        alerts_module.resolve_system(bridge.familyId, "sms_low", bridge.id)
        alerts_module.resolve_system(bridge.familyId, "sms_out", bridge.id)
    else:
        escalating = state.level == "low" and target == "out"
        track, announce = notices.enter_bad(track, target, now_s)
        if announce or escalating:
            if target == "out":
                notices.notify(
                    bridge.familyId, "sms_out", OUT_TEXT, key=bridge.id, since=now_s, broker=broker
                )
                alerts_module.resolve_system(bridge.familyId, "sms_low", bridge.id)
            else:
                notices.notify(
                    bridge.familyId, "sms_low", low_text(left or 0), key=bridge.id,
                    since=now_s, broker=broker,
                )
            notified = now_s
        if state.level == "out" and target == "low":
            alerts_module.resolve_system(bridge.familyId, "sms_out", bridge.id)
    bridges_store.update_carrier(
        bridge.id,
        lambda s: s.model_copy(
            update={"level": target, "levelSince": track.since, "levelNotifiedAt": notified}
        ),
    )
    return target


def _pending_carrier_items(bridge_id: str, prefix: str = bridge_outbox.CARRIER_PREFIX):
    return [i for i in bridge_outbox.list_pending(bridge_id) if i.id.startswith(prefix)]


def enqueue_check(
    bridge: Bridge, *, now: int | None = None, manual: bool = False
) -> bridge_outbox.OutboxItem | None:
    """Queue the balance command; `None` if not due, a manual check raises `CheckRefused`."""
    now_s = int(now if now is not None else time.time())
    carrier = resolve(bridge)
    command = carrier.balance_command()
    if command is None:
        if manual:
            raise CheckRefused(
                409, f"{carrier.name} has no balance command; enter the number by hand"
            )
        return None
    if command.method != "sms":
        if manual:
            raise CheckRefused(409, "this carrier's balance check needs USSD, which is not supported")
        return None
    if not bridge.paired:
        if manual:
            raise CheckRefused(409, "the bridge phone is not paired")
        return None
    if not bridge.caps.sms:
        if manual:
            raise CheckRefused(409, "the bridge phone has no SMS to ask with")
        return None
    state = state_of(bridge)
    if manual:
        if state.lastCheckAt is not None and now_s - state.lastCheckAt < MANUAL_CHECK_MIN_GAP_S:
            raise CheckRefused(429, "a check was just sent; give the carrier a few minutes")
    else:
        due = (
            state.checkDue
            or state.partsSinceCheck >= CHECK_EVERY_PARTS
            or state.lastCheckAt is None
            or now_s - state.lastCheckAt >= CHECK_EVERY_S
        )
        if not due:
            return None
        if state.lastCheckAt is not None and now_s - state.lastCheckAt < CHECK_MIN_GAP_S:
            return None
        if estimate_only(state):
            return None
    pending = _pending_carrier_items(bridge.id, CHECK_PREFIX)
    if pending:
        if manual:
            raise CheckRefused(409, "a check is already waiting for the phone")
        return None
    ob_id = new_id(CHECK_PREFIX)
    item = bridge_outbox.enqueue_carrier(bridge, ob_id, phone=command.to, text=command.text)
    carrier_log.append(
        bridge.id, f"out_{ob_id}", dir="out", party=command.to, text=command.text, kind="check"
    )

    def fn(s: CarrierState) -> CarrierState:
        return s.model_copy(
            update={
                "lastCheckAt": now_s,
                "checkDue": False,
                "partsSinceCheck": 0,
                "awaiting": True,
                "misses": s.misses + 1 if s.awaiting else s.misses,
            }
        )

    bridges_store.update_carrier(bridge.id, fn)
    return item


def tick(*, now: int | None = None, broker: BrokerClient | None = None) -> int:
    """The tick's pass over every paired bridge; returns how many checks were queued."""
    now_s = int(now if now is not None else time.time())
    queued = 0
    for bridge in bridges_store.list_all():
        if not bridge.paired:
            continue
        try:
            evaluate_level(bridge.id, now=now_s, broker=broker)
            if enqueue_check(bridge, now=now_s) is not None:
                queued += 1
            topups.expire_unconfirmed(bridge.id, now=now_s)
        except Exception:
            logger.exception("carrier tick failed bridge=%s", bridge.id)
    return queued
