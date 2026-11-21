"""System notices: one web alert plus a pager `system` text (PROTOCOL.md §4.2) per state change."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from pydantic import BaseModel

from app import alerts as alerts_module
from app import book, sysmsg, wire
from app.broker import BrokerClient
from app.store import devices as devices_store
from app.store import families as families_store

logger = logging.getLogger("relay.notices")

# Pager text limit in bytes.
NOTICE_MAX_BYTES = 90
# A "down" for the same key is not announced again inside this window.
HOLD_OFF_S = 60 * 60

# Known codes; `notify` accepts any `[a-z_]` code.
CODES = frozenset(
    {
        "bridge_offline",
        "bridge_online",
        "bridge_degraded",
        "bridge_battery",
        "sms_low",
        "sms_out",
        "sms_topped_up",
        "peer_down",
        "peer_up",
        "link_unreachable",
        "family_frozen",
    }
)


@dataclass
class NoticeResult:
    alertId: str
    # Pagers the text was published to (broker accepted it).
    pagers: list[str] = field(default_factory=list)
    # Pagers it should have gone to but the broker refused.
    pagerFailed: list[str] = field(default_factory=list)


class Health(BaseModel):
    """`health{state, since, notifiedAt}`; `since` starts the outage, `notifiedAt` is the last down."""

    state: str = "ok"
    since: int = 0
    notifiedAt: int | None = None


def enter_bad(health: Health, state: str, now: int) -> tuple[Health, bool]:
    """Return `(new health, announce)`; announce unless a down went out within `HOLD_OFF_S`."""
    if health.state == state:
        return health, False
    since = health.since if health.state != "ok" else now
    announce = health.notifiedAt is None or now - health.notifiedAt >= HOLD_OFF_S
    return (
        Health(
            state=state, since=since, notifiedAt=now if announce else health.notifiedAt
        ),
        announce,
    )


def enter_ok(health: Health, now: int) -> tuple[Health, bool]:
    """Return `(new health, announce)`; a restore is announced only after an announced down."""
    if health.state == "ok":
        return health, False
    announce = health.notifiedAt is not None and health.notifiedAt >= health.since
    return Health(state="ok", since=now, notifiedAt=health.notifiedAt), announce


def _has_bridged_contact(owner_uid: str) -> bool:
    return any(
        e.kind == "external" and book.listed_on_pager(e)
        for e in book.entries_for(owner_uid)
    )


def _pager_targets(family_id: str) -> list[str]:
    return [
        d.id
        for d in devices_store.list_devices()
        if d.familyId == family_id
        and d.revokedAt is None
        and _has_bridged_contact(d.ownerUid)
    ]


def notify(
    family_id: str,
    code: str,
    text: str,
    *,
    key: str,
    resolves: str | None = None,
    pager: bool = True,
    device_ids: list[str] | None = None,
    since: int | None = None,
    broker: BrokerClient | None = None,
) -> NoticeResult:
    """Upsert the web alert and publish the pager text; a refused publish is reported, not raised."""
    text = " ".join(wire.CONTROL_CHAR_RE.sub(" ", text).split())
    if not text:
        raise ValueError("notice text must not be empty")
    if resolves is not None:
        alerts_module.resolve_system(family_id, resolves, key)
    alert_id = alerts_module.system_notice(
        family_id, code, key, text, closed=resolves is not None
    )
    result = NoticeResult(alertId=alert_id)
    if not pager:
        return result
    family = families_store.get_family(family_id)
    if family is None or not family.settings.notices.pager:
        logger.info(
            "notice code=%s key=%s family=%s pager copy off", code, key, family_id
        )
        return result
    client = broker or sysmsg.get_broker()
    if client is None:
        logger.warning(
            "notice code=%s key=%s: no broker client; pager copy not sent", code, key
        )
        return result
    body = sysmsg.clip_bytes(text, NOTICE_MAX_BYTES)
    if body != text:
        logger.warning(
            "notice code=%s key=%s text over %d bytes; clipped",
            code,
            key,
            NOTICE_MAX_BYTES,
        )
    epoch = str(since if since is not None else int(time.time()))
    targets = device_ids if device_ids is not None else _pager_targets(family_id)
    for device_id in targets:
        mid = sysmsg.derived_id("notice", device_id, code, key, epoch)
        if sysmsg.send_system_text(client, device_id, body, msg_id=mid):
            result.pagers.append(device_id)
        else:
            result.pagerFailed.append(device_id)
    logger.info(
        "notice code=%s key=%s family=%s alert=%s pagers=%d failed=%d",
        code,
        key,
        family_id,
        alert_id,
        len(result.pagers),
        len(result.pagerFailed),
    )
    return result
