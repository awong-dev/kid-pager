"""Top-ups: `request` re-checks the family policy in the transaction that writes the row.
The carrier's reply settles it; with no reply after 15 minutes it is `unconfirmed` and counts."""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime, timedelta

from pydantic import BaseModel

from app import clockfmt
from app.carriers import registry
from app.carriers.base import Pack, TopupFailed, TopupOk
from app.ids import new_id
from app.store import bridge_outbox, carrier_log
from app.store import bridges as bridges_store
from app.store import families as families_store
from app.store import topups as topups_store
from app.store.topups import Refused, TopupDoc

logger = logging.getLogger("relay.topups")

UNCONFIRMED_AFTER_S = 15 * 60
OUTBOX_PREFIX = "ob_cr_tu_"

__all__ = ["Refused", "TopupDoc", "allowance", "expire_unconfirmed", "month_bounds", "request"]


class Allowance(BaseModel):
    """Whether a top-up may be pressed now, and why not (the web shows `reason`)."""

    mode: str
    perMonth: int | None
    used: int
    allowed: bool
    reason: str | None
    packs: list[dict]


def month_bounds(family_id: str, now: datetime) -> tuple[datetime, datetime]:
    """The calendar month containing `now` in the family zone, as UTC instants."""
    zone = clockfmt.zone_for(family_id)
    local = now.astimezone(zone)
    start = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    nxt = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
    return start.astimezone(UTC), nxt.astimezone(UTC)


def _pack(bridge: bridges_store.Bridge, pack_id: str) -> Pack | None:
    carrier = registry.get(bridge.carrier.id if bridge.carrier else None)
    return next((p for p in carrier.topup_packs() if p.id == pack_id), None)


def _limit(family_id: str) -> tuple[str, int | None]:
    family = families_store.get_family(family_id)
    policy = family.settings.topup if family is not None else families_store.TopupSettings()
    return policy.mode, (policy.perMonth if policy.mode == "limited" else None)


def allowance(bridge: bridges_store.Bridge, *, now: datetime | None = None) -> Allowance:
    now = now or datetime.now(UTC)
    mode, limit = _limit(bridge.familyId)
    start, end = month_bounds(bridge.familyId, now)
    used = topups_store.count_between(bridge.id, start, end)
    carrier = registry.get(bridge.carrier.id if bridge.carrier else None)
    packs = [{"id": p.id, "label": p.label, "texts": p.texts} for p in carrier.topup_packs()]
    reason: str | None = None
    if mode == "off":
        reason = "top-ups are off for this family"
    elif not packs:
        reason = f"{carrier.name} has no top-up"
    elif not bridge.paired:
        reason = "the bridge phone is not paired"
    elif limit is not None and used >= limit:
        reason = f"{used} of {limit} used this month"
    elif any(r.state == "requested" for r in topups_store.list_recent(bridge.id, limit=5)):
        reason = "a top-up is already waiting for the carrier's reply"
    return Allowance(
        mode=mode, perMonth=limit, used=used, allowed=reason is None, reason=reason, packs=packs
    )


def request(
    bridge: bridges_store.Bridge, pack_id: str, by_uid: str, *, now: datetime | None = None
) -> TopupDoc:
    """Queues a top-up and returns the `requested` row; raises `Refused` (403, 404 or 409)."""
    now = now or datetime.now(UTC)
    pack = _pack(bridge, pack_id)
    if pack is None:
        raise Refused(404, "no such top-up pack for this carrier")
    if pack.command.method != "sms":
        raise Refused(409, "this top-up needs USSD, which is not supported")
    if not bridge.paired or not bridge.caps.sms:
        raise Refused(409, "the bridge phone cannot send texts right now")
    mode, limit = _limit(bridge.familyId)
    if mode == "off":
        raise Refused(403, "top-ups are off for this family")
    start, end = month_bounds(bridge.familyId, now)
    topup_id = new_id("t_")
    doc = topups_store.request(
        bridge.id,
        topup_id,
        pack=pack.id,
        pack_label=pack.label,
        texts=pack.texts,
        by=by_uid,
        now=now,
        month_start=start,
        month_end=end,
        limit=limit,
    )
    ob_id = f"{OUTBOX_PREFIX}{topup_id}"
    try:
        bridge_outbox.enqueue_carrier(bridge, ob_id, phone=pack.command.to, text=pack.command.text)
        topups_store.set_state(
            bridge.id, topup_id, "requested", only_from=("requested",), outbox_id=ob_id
        )
        carrier_log.append(
            bridge.id, f"out_{ob_id}", dir="out", party=pack.command.to, text=pack.command.text,
            kind="topup", detail=pack.id,
        )
    except Exception:
        logger.exception("top-up command not queued bridge=%s topup=%s", bridge.id, topup_id)
        topups_store.set_state(
            bridge.id, topup_id, "failed", only_from=("requested",), note="not queued"
        )
        raise Refused(503, "could not queue the top-up; nothing was bought") from None
    logger.info("topup requested bridge=%s topup=%s pack=%s by=%s", bridge.id, topup_id, pack.id, by_uid)
    return topups_store.get(bridge.id, topup_id) or doc


def on_reply(bridge: bridges_store.Bridge, event: TopupOk | TopupFailed, now: int) -> None:
    """The carrier answered a top-up. Settles the newest waiting top-up (a late reply also
    settles an `unconfirmed` one)."""
    rows = topups_store.list_recent(bridge.id, limit=20)
    target = next((r for r in rows if r.state == "requested"), None) or next(
        (r for r in rows if r.state == "unconfirmed"), None
    )
    if target is None:
        return
    if isinstance(event, TopupOk):
        topups_store.set_state(
            bridge.id, target.id, "confirmed", only_from=("requested", "unconfirmed")
        )
    else:
        topups_store.set_state(
            bridge.id, target.id, "failed", only_from=("requested", "unconfirmed"),
            note=event.reason[:120] or None,
        )


def on_command_failed(bridge: bridges_store.Bridge, topup_id: str, reason: str | None) -> None:
    """The phone could not send the command: nothing was bought."""
    topups_store.set_state(
        bridge.id, topup_id, "failed", only_from=("requested",), note=reason or "not sent"
    )


def expire_unconfirmed(bridge_id: str, *, now: int | None = None) -> int:
    """Sent rows with no reply after 15 minutes become `unconfirmed`; failed ones `failed`."""
    now_s = int(now if now is not None else time.time())
    changed = 0
    for row in topups_store.list_recent(bridge_id, limit=20):
        if row.state != "requested":
            continue
        item = bridge_outbox.get(bridge_id, f"{OUTBOX_PREFIX}{row.id}")
        if item is not None and item.state == "pending":
            continue
        if item is not None and item.state == "failed":
            changed += topups_store.set_state(
                bridge_id, row.id, "failed", only_from=("requested",), note=item.reason or "not sent"
            )
            continue
        started = item.ackedAt if item is not None and item.ackedAt is not None else row.at
        if now_s - int(topups_store.aware(started).timestamp()) >= UNCONFIRMED_AFTER_S:
            changed += topups_store.set_state(
                bridge_id, row.id, "unconfirmed", only_from=("requested",),
                note="no reply from the carrier",
            )
    return changed
