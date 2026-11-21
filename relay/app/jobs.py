"""`/internal/tick`'s and `/internal/sweep`'s scheduled work: retries queued pager deliveries, fails
stale bridge outbox rows, and the retention sweep over every collection class."""

from __future__ import annotations

import logging
import os
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta

from google.cloud.firestore import (
    DELETE_FIELD,
    Client,
    CollectionGroup,
    FieldFilter,
    Query,
    WriteBatch,
)

from app import bridge_health, deliveries, link_health, location
from app.authn_home import SESSION_IDLE
from app.broker import BrokerClient
from app.carriers import service as carriers_service
from app.db.firestore import get_db
from app.fed import sender as fed_sender
from app.routing import Routing
from app.store import bridge_outbox
from app.store import bridges as bridges_store
from app.store import devices as devices_store
from app.store import messages as messages_store
from app.store import settings as settings_store
from app.tasks import TaskQueue, build_task_queue

logger = logging.getLogger("relay.jobs")

BRIDGE_OFFLINE_AFTER = timedelta(hours=24)
BRIDGE_ACKED_RETENTION = timedelta(days=7)


@dataclass(frozen=True, slots=True)
class TickResult:
    devicesChecked: int
    retriesAttempted: int
    locReqsCleared: int = 0
    bridgeOutboxFailed: int = 0


def _fail_stale_bridge_outbox() -> int:
    """A `pending` outbox row older than 24 h on any bridge fails `bridge_offline`, with its delivery."""
    cutoff = datetime.now(UTC) - BRIDGE_OFFLINE_AFTER
    failed = 0
    for item in bridge_outbox.list_stale_pending_all(cutoff):
        acked = bridge_outbox.ack(item.bridgeId or "", item.id, "failed", "bridge_offline", 0)
        if acked is None or not acked[1]:
            continue
        failed += 1
        if item.msgId and item.bid:
            deliveries.mark_failed(item.msgId, item.bid, error="bridge_offline")
    return failed


def tick(
    routing: Routing,
    *,
    task_queue: TaskQueue | None = None,
    broker: BrokerClient | None = None,
) -> TickResult:
    task_queue = task_queue if task_queue is not None else build_task_queue()
    devices_checked = 0
    retries_attempted = 0

    for device in devices_store.list_devices():
        if device.revokedAt is not None:
            continue
        devices_checked += 1

        for msg in messages_store.list_pending_for_device(device.id):
            bid = messages_store.find_pager_delivery(msg, device.id)
            if bid is None:
                continue
            delivery = msg.deliveries.get(bid)
            if delivery is None or delivery.state != "queued":
                # 'sent' (broker accepted it, no ack yet) is the online edge's job
                # (docs/PROTOCOL.md §5.3); only a publish that never reached the broker is ours.
                continue
            retries_attempted += 1
            task_queue.enqueue(
                lambda msg=msg, device_id=device.id: routing.redeliver_pager(msg, device_id),
                name=f"pager-retry:{msg.id}:{device.id}",
            )

    loc_reqs_cleared = location.clear_stale_loc_reqs()
    # The location class runs on the first tick of every hour (a fix outlives its limit by <= 1 h).
    if loc_pass_due(datetime.now(UTC)):
        location_pass()
    bridge_rows_failed = _fail_stale_bridge_outbox()
    # Offline edges for every paired bridge.
    bridge_health.evaluate_all(broker=broker)
    # Carrier balance checks and low/out edges.
    try:
        carriers_service.tick(broker=broker)
    except Exception:
        logger.exception("carrier tick failed")
    # Active link probes, at most one per bridge per hour.
    try:
        link_health.enqueue_probes()
    except Exception:
        logger.exception("link probes failed")

    # Retry pending peer posts, fail those pending past FED_OFFLINE_AFTER_S, peer up/down notices,
    # idle pings.
    fed_sender.tick_pass(broker=broker)

    return TickResult(
        bridgeOutboxFailed=bridge_rows_failed,
        devicesChecked=devices_checked,
        retriesAttempted=retries_attempted,
        locReqsCleared=loc_reqs_cleared,
    )


DEFAULT_SWEEP_BATCH = 500
# Firestore's own hard cap on writes in one `WriteBatch.commit()`.
MAX_BATCH_WRITES = 500


def _sweep_batch_size() -> int:
    """Query page size per collection per iteration; `SWEEP_BATCH` is read on every call."""
    return int(os.environ.get("SWEEP_BATCH", str(DEFAULT_SWEEP_BATCH)))


def _cutoff(seconds: int) -> datetime:
    return datetime.fromtimestamp(time.time() - seconds, tz=UTC)


class _AutoBatch:
    """Wraps a `WriteBatch`, committing every `MAX_BATCH_WRITES` deletes (Firestore's cap is 500 per
    batch), independent of the query page size."""

    def __init__(self, db: Client) -> None:
        self._db = db
        self._batch: WriteBatch = db.batch()
        self._count = 0

    def reserve(self, n: int) -> None:
        """Commit now if the next `n` deletes would not fit, so a group (a message and its `wireIds`
        companion) is never split across commits; a split could orphan the companion."""
        if self._count and self._count + n > MAX_BATCH_WRITES:
            self._batch.commit()
            self._batch = self._db.batch()
            self._count = 0

    def delete(self, ref) -> None:
        self._batch.delete(ref)
        self._count += 1
        if self._count >= MAX_BATCH_WRITES:
            self._batch.commit()
            self._batch = self._db.batch()
            self._count = 0

    def update(self, ref, data: dict) -> None:
        self._batch.update(ref, data)
        self._count += 1
        if self._count >= MAX_BATCH_WRITES:
            self._batch.commit()
            self._batch = self._db.batch()
            self._count = 0

    def commit(self) -> None:
        if self._count:
            self._batch.commit()
            self._batch = self._db.batch()
            self._count = 0


def _sweep_by_created_at(
    query_factory,
    cutoff: datetime,
    batch_size: int,
    *,
    on_doc=None,
    created_at_field: str = "createdAt",
) -> int:
    """Delete every doc older than `cutoff`, one query page and `WriteBatch` per iteration.
    `on_doc(batch, snap)` stages extra deletes (the `wireIds` companion) in the same commit;
    `created_at_field` names the timestamp field compared."""
    db = get_db()
    deleted = 0
    while True:
        query: Query | CollectionGroup = (
            query_factory()
            .where(filter=FieldFilter(created_at_field, "<", cutoff))
            .order_by(created_at_field)
            .limit(batch_size)
        )
        docs = list(query.stream())
        if not docs:
            break
        batch = _AutoBatch(db)
        group = 1 if on_doc is None else 2
        for snap in docs:
            # Keep each doc and its companion in one commit (`_AutoBatch.reserve`).
            batch.reserve(group)
            batch.delete(snap.reference)
            deleted += 1
            if on_doc is not None:
                on_doc(batch, snap)
        batch.commit()
        if len(docs) < batch_size:
            # A short page means the collection is exhausted.
            break
    return deleted


def _sweep_alerts(cutoff: datetime, batch_size: int) -> int:
    """`families/{fid}/alerts/{id}` docs with `ts < cutoff`; open alerts are included because they
    can hold a held-SMS body and a peer phone number."""
    return _sweep_by_created_at(
        lambda: get_db().collection_group("alerts"), cutoff, batch_size, created_at_field="ts"
    )


def _sweep_expired(
    query_factory, now_epoch_s: int, batch_size: int, *, expires_field: str = "expiresAt"
) -> int:
    """Like `_sweep_by_created_at` for collections with their own Unix-epoch-seconds expiry field:
    deletes every doc whose `expires_field` is `< now_epoch_s`. No companion deletes."""
    db = get_db()
    deleted = 0
    while True:
        query: Query | CollectionGroup = (
            query_factory()
            .where(filter=FieldFilter(expires_field, "<", now_epoch_s))
            .order_by(expires_field)
            .limit(batch_size)
        )
        docs = list(query.stream())
        if not docs:
            break
        batch = _AutoBatch(db)
        for snap in docs:
            batch.reserve(1)
            batch.delete(snap.reference)
            deleted += 1
        batch.commit()
        if len(docs) < batch_size:
            break
    return deleted


@dataclass(frozen=True, slots=True)
class SweepResult:
    messagesDeleted: int
    wireIdsDeleted: int
    locationsDeleted: int
    locWireIdsDeleted: int
    locReqsDeleted: int
    orphanedWireIdsDeleted: int = 0
    conversationsDeleted: int = 0
    gchatLinkCodesDeleted: int = 0
    alertsDeleted: int = 0
    batteryDeleted: int = 0
    heldSmsDeleted: int = 0
    bridgePairCodesDeleted: int = 0
    bridgeOutboxDeleted: int = 0
    heldChatDeleted: int = 0
    sessionsDeleted: int = 0
    inviteCodesDeleted: int = 0
    webauthnChallengesDeleted: int = 0
    smsLogDeleted: int = 0
    topupsDeleted: int = 0
    rateLimitsDeleted: int = 0
    cellsDeleted: int = 0
    contactRequestsDeleted: int = 0
    setupCodesDeleted: int = 0
    locMessagesScrubbed: int = 0
    deviceStatusScrubbed: int = 0
    fedInvitesDeleted: int = 0
    fedOutboxDeleted: int = 0
    # oldest surviving `createdAt` per class (None = class empty)
    oldest: dict[str, datetime | None] | None = None


TOPUP_KEEP = timedelta(days=400)
RATE_LIMIT_KEEP_S = 86400
CELL_CACHE_MAX_AGE = timedelta(days=30)
CONTACT_REQUEST_MAX_AGE = timedelta(days=90)
FED_OUTBOX_RETENTION = timedelta(days=7)
LOC_EXPIRED_BODY = "location (expired)"


def _scrub_loc_messages(cutoff: datetime, batch_size: int) -> int:
    """Strip the coordinates from `kind:"loc"` messages older than the location limit, keeping the
    message as "location (expired)". Filters in Python after a `kind` equality stream."""
    db = get_db()
    scrubbed = 0
    batch = _AutoBatch(db)
    for snap in db.collection("messages").where(filter=FieldFilter("kind", "==", "loc")).stream():
        data = snap.to_dict() or {}
        created = data.get("createdAt")
        if data.get("loc") is None or created is None or created >= cutoff:
            continue
        batch.update(snap.reference, {"loc": None, "body": LOC_EXPIRED_BODY, "locExpired": True})
        scrubbed += 1
    batch.commit()
    return scrubbed


def _scrub_device_status(cutoff: datetime) -> int:
    """`devices/{id}.status.lastCell` is a position; drop it once older than the location limit."""
    db = get_db()
    cutoff_s = cutoff.timestamp()
    scrubbed = 0
    for snap in db.collection("devices").stream():
        status = (snap.to_dict() or {}).get("status") or {}
        cell = status.get("lastCell")
        if not cell:
            continue
        ts = cell.get("ts") if cell.get("ts") is not None else status.get("ts")
        if ts is not None and ts >= cutoff_s:
            continue
        snap.reference.set({"status": {"lastCell": DELETE_FIELD}}, merge=True)
        scrubbed += 1
    return scrubbed


def location_pass(*, now: datetime | None = None) -> dict[str, int]:
    """The `location` retention class, run from `sweep()` and the first tick of every hour."""
    db = get_db()
    batch_size = _sweep_batch_size()
    cutoff = _cutoff(settings_store.get_retention().locationsDays * 86400)
    counts = {
        "locationsDeleted": _sweep_by_created_at(
            lambda: db.collection_group("locations"), cutoff, batch_size
        ),
        "locWireIdsDeleted": _sweep_by_created_at(
            lambda: db.collection("locWireIds"), cutoff, batch_size
        ),
        "locReqsDeleted": _sweep_by_created_at(
            lambda: db.collection("locReqs"), cutoff, batch_size
        ),
        "locMessagesScrubbed": _scrub_loc_messages(cutoff, batch_size),
        "deviceStatusScrubbed": _scrub_device_status(cutoff),
    }
    settings_store.mark_loc_pass()
    return counts


def loc_pass_due(now: datetime) -> bool:
    """True on the first tick of each hour: no pass recorded yet, or the last fell in an earlier hour."""
    last = settings_store.get_meta().lastLocPassAt
    if last is None:
        return True
    return last.astimezone(UTC).replace(minute=0, second=0, microsecond=0) < now.astimezone(
        UTC
    ).replace(minute=0, second=0, microsecond=0)


def _oldest(query_factory, field: str = "createdAt") -> datetime | None:
    docs = list(query_factory().order_by(field).limit(1).stream())
    if not docs:
        return None
    value = (docs[0].to_dict() or {}).get(field)
    return value if isinstance(value, datetime) else None


def sweep() -> SweepResult:
    db = get_db()
    retention = settings_store.get_retention()
    batch_size = _sweep_batch_size()
    msg_cutoff = _cutoff(retention.messagesDays * 86400)

    wire_ids_deleted = 0

    def _delete_companion_wire_id(batch: _AutoBatch, snap) -> None:
        nonlocal wire_ids_deleted
        data = snap.to_dict() or {}
        wire_id = data.get("wireId")
        recipient_uid = data.get("recipientUid")
        if wire_id and recipient_uid:
            batch.delete(db.collection("wireIds").document(f"{wire_id}_{recipient_uid}"))
            wire_ids_deleted += 1

    # Location class first (the strictest), so a later failure cannot leave it behind; `loc`
    # messages are scrubbed before the message pass deletes any also past the message limit.
    loc = location_pass()

    messages_deleted = _sweep_by_created_at(
        lambda: db.collection("messages"),
        msg_cutoff,
        batch_size,
        on_doc=_delete_companion_wire_id,
    )
    # `wireIds` docs are also swept by `messagesDays`, so a doc orphaned by any other deletion path
    # is reclaimed; the paired pass above has already removed its own.
    orphaned_wire_ids_deleted = _sweep_by_created_at(
        lambda: db.collection("wireIds"), msg_cutoff, batch_size
    )
    # `conversations/{convKey}` summaries (`lastPreview` is a truncated body)
    # inherit the message class, by `lastMessageAt`.
    conversations_deleted = _sweep_by_created_at(
        lambda: db.collection("conversations"),
        msg_cutoff,
        batch_size,
        created_at_field="lastMessageAt",
    )
    # `devices/*/smsLog`: SMS bodies and numbers, the message class.
    sms_log_deleted = _sweep_by_created_at(
        lambda: db.collection_group("smsLog"), msg_cutoff, batch_size, created_at_field="receivedAt"
    )
    # `gchatLinkCodes` -- swept by its own `expiresAt` (an int epoch, already-expired cutoff).
    gchat_link_codes_deleted = _sweep_expired(
        lambda: db.collection("gchatLinkCodes"), int(time.time()), batch_size
    )
    # `families/{fid}/alerts/{id}` -- `alertsDays`, open alerts included.
    alerts_deleted = _sweep_alerts(_cutoff(retention.alertsDays * 86400), batch_size)

    # `devices/{id}/battery` samples.
    battery_deleted = _sweep_by_created_at(
        lambda: db.collection_group("battery"),
        _cutoff(retention.batteryDays * 86400),
        batch_size,
    )

    # Held inbound texts live for the message retention window (by `receivedAt`).
    held_sms_deleted = _sweep_by_created_at(
        lambda: db.collection("heldSms"), msg_cutoff, batch_size, created_at_field="receivedAt"
    )

    # Held Chat texts likewise.
    held_chat_deleted = _sweep_by_created_at(
        lambda: db.collection("heldChat"), msg_cutoff, batch_size, created_at_field="receivedAt"
    )

    # Expired pairing codes.
    bridge_codes_deleted = 0
    for code in bridges_store.list_expired_pair_codes(datetime.now(UTC)):
        bridges_store.delete_pair_code(code)
        bridge_codes_deleted += 1

    outbox_deleted = bridge_outbox.delete_acked_before(datetime.now(UTC) - BRIDGE_ACKED_RETENTION)

    # Top-up history, 400 days (by `at`).
    topups_deleted = _sweep_by_created_at(
        lambda: db.collection_group("topups"), datetime.now(UTC) - TOPUP_KEEP, batch_size,
        created_at_field="at",
    )

    # Ephemeral class: sessions idle past `SESSION_IDLE`, invite codes,
    # WebAuthn challenges and device setup codes past their own `expiresAt`.
    now = datetime.now(UTC)
    sessions_deleted = _sweep_by_created_at(
        lambda: db.collection("sessions"), now - SESSION_IDLE, batch_size, created_at_field="lastSeenAt"
    )
    invite_codes_deleted = _sweep_by_created_at(
        lambda: db.collection("inviteCodes"), now, batch_size, created_at_field="expiresAt"
    )
    challenges_deleted = _sweep_by_created_at(
        lambda: db.collection("webauthnChallenges"), now, batch_size, created_at_field="expiresAt"
    )
    setup_codes_deleted = _sweep_by_created_at(
        lambda: db.collection("setupCodes"), now, batch_size, created_at_field="expiresAt"
    )
    # `rateLimits/{key}` doc ids embed client IPs; a window older than a day
    # protects nothing and identifies a visitor (`windowStart` is epoch seconds).
    rate_limits_deleted = _sweep_expired(
        lambda: db.collection("rateLimits"),
        int(time.time()) - RATE_LIMIT_KEEP_S,
        batch_size,
        expires_field="windowStart",
    )
    contact_requests_deleted = _sweep_by_created_at(
        lambda: db.collection("contactRequests"), now - CONTACT_REQUEST_MAX_AGE, batch_size
    )
    # Cache class: resolved cell positions older than 30 days.
    cells_deleted = _sweep_by_created_at(
        lambda: db.collection("cells"), now - CELL_CACHE_MAX_AGE, batch_size, created_at_field="resolvedAt"
    )

    fed_invites_deleted = _sweep_expired(
        lambda: db.collection("fedInvites"), int(time.time()), batch_size
    )
    # `fedOutbox` rows that are not pending carry `ackedAt` (acked and failed alike).
    fed_outbox_deleted = _sweep_by_created_at(
        lambda: db.collection("fedOutbox"),
        now - FED_OUTBOX_RETENTION,
        batch_size,
        created_at_field="ackedAt",
    )

    oldest = {
        "messages": _oldest(lambda: db.collection("messages")),
        "locations": _oldest(lambda: db.collection_group("locations")),
    }

    result = SweepResult(
        messagesDeleted=messages_deleted,
        wireIdsDeleted=wire_ids_deleted,
        locationsDeleted=loc["locationsDeleted"],
        locWireIdsDeleted=loc["locWireIdsDeleted"],
        locReqsDeleted=loc["locReqsDeleted"],
        orphanedWireIdsDeleted=orphaned_wire_ids_deleted,
        conversationsDeleted=conversations_deleted,
        gchatLinkCodesDeleted=gchat_link_codes_deleted,
        alertsDeleted=alerts_deleted,
        batteryDeleted=battery_deleted,
        heldSmsDeleted=held_sms_deleted,
        bridgePairCodesDeleted=bridge_codes_deleted,
        bridgeOutboxDeleted=outbox_deleted,
        heldChatDeleted=held_chat_deleted,
        sessionsDeleted=sessions_deleted,
        inviteCodesDeleted=invite_codes_deleted,
        webauthnChallengesDeleted=challenges_deleted,
        smsLogDeleted=sms_log_deleted,
        topupsDeleted=topups_deleted,
        rateLimitsDeleted=rate_limits_deleted,
        cellsDeleted=cells_deleted,
        contactRequestsDeleted=contact_requests_deleted,
        setupCodesDeleted=setup_codes_deleted,
        locMessagesScrubbed=loc["locMessagesScrubbed"],
        deviceStatusScrubbed=loc["deviceStatusScrubbed"],
        fedInvitesDeleted=fed_invites_deleted,
        fedOutboxDeleted=fed_outbox_deleted,
        oldest=oldest,
    )
    counts = {k: v for k, v in asdict(result).items() if isinstance(v, int)}
    settings_store.mark_swept({"at": now, "counts": counts, "oldest": oldest})
    # On the home store: return free pages and truncate the WAL (the Firestore client has no such
    # method).
    reclaim = getattr(db, "reclaim_space", None)
    if reclaim is not None:
        logger.info("sweep space reclaim: %s", reclaim())

    logger.info("sweep complete: %s", counts)
    return result
