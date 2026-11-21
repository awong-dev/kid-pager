"""Subscribed link checks: passive strikes from acks and inbound events, active `inspect` probes
from `tick`; two strikes at least an hour apart make the link `unreachable`."""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime

from app import alerts as alerts_module
from app import notices
from app.broker import BrokerClient
from app.store import bridge_conversations as conv_store
from app.store import bridge_outbox
from app.store import bridges as bridges_store
from app.store import families as families_store
from app.store.bridge_conversations import BridgeConversation

logger = logging.getLogger("relay.link_health")

PROBE_PREFIX = "ob_hc_"
# Failure reasons that say something about the link.
LINK_FAILURES = frozenset({"no_link", "no_match", "ui_changed", "app_missing"})
PROBE_SOURCES = frozenset({"gchat", "gvoice"})
CHAT_SOURCES = frozenset({"gchat", "gvoice", "whatsapp"})
INBOUND_REFRESH_S = 3600
STALE_AFTER_S = 7 * 86400
PROBE_EVERY_S = 3600
# A probe marker older than this is abandoned (its result never came).
PROBE_WAIT_S = 2 * 3600
SOURCE_LABELS = {"gchat": "Chat", "gvoice": "Voice thread", "whatsapp": "WhatsApp chat"}


def is_probe_id(ob_id: str) -> bool:
    return ob_id.startswith(PROBE_PREFIX)


def probe_id(ref: str, now: int) -> str:
    return f"{PROBE_PREFIX}{ref}_{datetime.fromtimestamp(now, tz=UTC):%Y%m%d}"


def titles_match(stored: str | None, seen: str | None) -> bool:
    """Opened and the title agrees: case-insensitive, either contains the other. A
    conversation with no stored title only has to open (with some title read)."""
    a = (stored or "").strip().casefold()
    b = (seen or "").strip().casefold()
    if not a:
        return True
    if not b:
        return False
    return a in b or b in a


def alert_key(row: BridgeConversation) -> str:
    return f"{row.bridgeId}_{row.ref}"


def _unreachable_text(row: BridgeConversation) -> str:
    title = (row.title or "a conversation").strip()
    if len(title) > 40:
        title = title[:39] + "..."
    label = SOURCE_LABELS.get(row.source, "Chat")
    return f"{label} '{title}' can't be opened on the bridge phone. Re-add it by link."


def _ok(row: BridgeConversation, now: int, *, min_interval_s: int = 0) -> None:
    changed = conv_store.record_ok(row.id, now, min_interval_s=min_interval_s)
    if changed is not None and changed[0].state == "unreachable":
        alerts_module.resolve_system(row.familyId, "link_unreachable", alert_key(row))


def _fail(row: BridgeConversation, reason: str, now: int, broker: BrokerClient | None) -> None:
    changed = conv_store.record_failing(row.id, reason, now)
    if changed is not None and changed[1].state == "unreachable" and changed[0].state != "unreachable":
        notices.notify(
            row.familyId,
            "link_unreachable",
            _unreachable_text(row),
            key=alert_key(row),
            pager=False,
            broker=broker,
        )


def on_inbound(row: BridgeConversation, *, now: int | None = None) -> None:
    """A message arrived for a subscribed conversation: the link works. Writes at most
    once an hour per row while it is already ok."""
    h = row.health
    now_s = int(now if now is not None else time.time())
    if h is not None and h.state == "ok" and h.okAt is not None and now_s - h.okAt < INBOUND_REFRESH_S:
        return
    _ok(row, now_s, min_interval_s=INBOUND_REFRESH_S)


def on_ack(
    bridge: bridges_store.Bridge,
    item: bridge_outbox.OutboxItem,
    *,
    now: int | None = None,
    broker: BrokerClient | None = None,
) -> None:
    """Called once when an outbox row transitions on an ack. Never raises into the ack."""
    now_s = int(now if now is not None else time.time())
    try:
        if is_probe_id(item.id):
            _probe_ack(bridge, item, now_s, broker)
            return
        conv_id = (item.to or {}).get("conversationId")
        if item.kind != "send" or item.source not in CHAT_SOURCES or not conv_id:
            return
        row = conv_store.get(bridge.id, conv_id)
        if row is None or row.status != "subscribed":
            return
        if item.state == "sent":
            _ok(row, now_s, min_interval_s=INBOUND_REFRESH_S)
        elif item.state == "failed" and item.reason in LINK_FAILURES:
            _fail(row, item.reason or "failed", now_s, broker)
    except Exception:
        logger.exception("link health ack hook failed bridge=%s ob=%s", bridge.id, item.id)


def _marked_row(bridge_id: str, *, ob_id: str | None = None, link: str | None = None):
    for row in conv_store.list_for_bridge(bridge_id):
        h = row.health
        if h is None or not h.probeId:
            continue
        if ob_id is not None and h.probeId == ob_id:
            return row
        if link is not None and row.link == link:
            return row
    return None


def _probe_ack(
    bridge: bridges_store.Bridge, item: bridge_outbox.OutboxItem, now: int, broker: BrokerClient | None
) -> None:
    if item.state != "failed":
        return  # `sent`: the result event decides
    row = _marked_row(bridge.id, ob_id=item.id)
    if row is None:
        return
    conv_store.set_probe(row.id, None, now)
    if item.reason in LINK_FAILURES:
        _fail(row, item.reason or "failed", now, broker)
    # `no_accessibility` and the like say nothing about the link.


def apply_probe_result(
    bridge: bridges_store.Bridge,
    link: str | None,
    title: str | None,
    *,
    now: int | None = None,
    broker: BrokerClient | None = None,
) -> str | None:
    """If an `inspect` result for `link` answers a probe of this bridge, apply it and
    return the event outcome (`probe_ok` / `probe_failing`); else `None` and the event
    takes the ordinary path."""
    if not link:
        return None
    now_s = int(now if now is not None else time.time())
    row = _marked_row(bridge.id, link=link)
    if row is None or row.health is None:
        return None
    if row.health.probeAt is None or now_s - row.health.probeAt > PROBE_WAIT_S:
        return None
    conv_store.set_probe(row.id, None, now_s)
    if titles_match(row.title, title):
        _ok(row, now_s)
        return "probe_ok"
    _fail(row, "no_match", now_s, broker)
    return "probe_failing"


def _candidate(row: BridgeConversation, now: int) -> bool:
    if row.source not in PROBE_SOURCES or not row.link:
        return False
    h = row.health
    if h is None:
        return True
    if h.probeAt is not None and h.probeId and now - h.probeAt < PROBE_WAIT_S:
        return False
    return h.okAt is None or now - h.okAt >= STALE_AFTER_S


def enqueue_probes(*, now: int | None = None) -> int:
    """At most one probe per bridge per hour; returns how many were enqueued. A family with nothing
    to probe costs no per-bridge reads."""
    now_s = int(now if now is not None else time.time())
    by_bridge: dict[str, list[BridgeConversation]] = {}
    for row in conv_store.list_subscribed():
        if _candidate(row, now_s):
            by_bridge.setdefault(row.bridgeId, []).append(row)
    enqueued = 0
    link_checks: dict[str, bool] = {}
    for bridge_id, rows in by_bridge.items():
        try:
            bridge = bridges_store.get(bridge_id)
            if bridge is None or not bridge.paired or not bridge.status.accessibility:
                continue
            if bridge.health is None or bridge.health.state != "ok":
                continue
            if bridge.linkProbeAt is not None and now_s - bridge.linkProbeAt < PROBE_EVERY_S:
                continue
            if bridge.familyId not in link_checks:
                family = families_store.get_family(bridge.familyId)
                link_checks[bridge.familyId] = family is not None and family.settings.linkChecks
            if not link_checks[bridge.familyId]:
                continue
            if bridge_outbox.count_pending(bridge.id) > 0:
                continue
            rows.sort(key=lambda r: (r.health.okAt if r.health and r.health.okAt else 0))
            for row in rows:
                ob_id = probe_id(row.ref, now_s)
                conv_store.set_probe(row.id, ob_id, now_s)
                if bridge_outbox.enqueue_probe(bridge, ob_id, row.link or "") is None:
                    conv_store.set_probe(row.id, None, now_s)  # already probed today
                    continue
                bridges_store.set_link_probe_at(bridge.id, now_s)
                enqueued += 1
                break
        except Exception:
            logger.exception("link probe failed bridge=%s", bridge_id)
    return enqueued
