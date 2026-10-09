"""The routing engine: resolves recipients, gates, hook-filters, stores and delivers messages."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from app import alerts as alerts_module
from app import policy as policy_module
from app.backends.base import Backend, DeliverResult
from app.backends.registry import build_registry
from app.broker import BrokerClient
from app.ids import new_id
from app.store import allow as allow_store
from app.store import backends as backends_store
from app.store import bridges as bridges_store
from app.store import conversations as conversations_store
from app.store import devices as devices_store
from app.store import messages as messages_store
from app.store import users as users_store
from app.store.backends import Backend as BackendRow
from app.store.conversations import Conversation
from app.store.messages import Delivery, Message, MessageKind
from app.store.users import User

logger = logging.getLogger("relay.routing")

# `sms_contact`: both sides external, or a contact that is disabled or in another family.
# `no_sms_number`: an external/person pair where the person has no smsNumber.
RejectReason = Literal[
    "unknown_alias",
    "not_allowed",
    "not_member",
    "policy_out",
    "policy_in",
    "sms_contact",
    "no_sms_number",
    "no_bridge",
]


@dataclass(frozen=True, slots=True)
class RejectedRecipient:
    alias: str | None
    uid: str | None
    reason: RejectReason


@dataclass(frozen=True, slots=True)
class SendResult:
    messages: list[Message] = field(default_factory=list)
    rejected: list[RejectedRecipient] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.messages) and not self.rejected


def _is_external(uid: str) -> bool:
    user = users_store.get_user(uid)
    return user is not None and user.kind == "external"


class Routing:
    def __init__(self, broker: BrokerClient, registry: dict[str, Backend] | None = None) -> None:
        self._broker = broker
        self._registry = registry if registry is not None else build_registry(broker)

    def send(
        self,
        *,
        sender_uid: str,
        recipient_alias: str | None,
        kind: MessageKind,
        origin_backend_kind: str,
        origin_backend_id: str | None = None,
        body: str | None = None,
        loc: dict | None = None,
        wire_id: str | None = None,
        device_id: str | None = None,
        ts: int | None = None,
        sender_alias: str | None = None,
    ) -> SendResult:
        ts = ts if ts is not None else int(time.time())

        if recipient_alias is not None:
            group = conversations_store.get_by_alias(recipient_alias)
            if group is not None:
                return self._send_group(
                    sender_uid=sender_uid,
                    group=group,
                    kind=kind,
                    body=body,
                    loc=loc,
                    origin_backend_kind=origin_backend_kind,
                    origin_backend_id=origin_backend_id,
                    wire_id=wire_id,
                    ts=ts,
                    sender_alias=sender_alias,
                )

        candidates, rejected = self._candidate_recipients(sender_uid, recipient_alias, device_id)

        for recipient_uid in candidates:
            sms_reason = self._sms_route_reject(sender_uid, recipient_uid)
            if sms_reason is not None:
                logger.warning(
                    "SECURITY sender %s cannot reach sms peer %s (alias=%r) reason=%s",
                    sender_uid,
                    recipient_uid,
                    recipient_alias,
                    sms_reason,
                )
                rejected.append(
                    RejectedRecipient(alias=recipient_alias, uid=recipient_uid, reason=sms_reason)
                )
                continue
            reason = self._policy_reject_reason(sender_uid, recipient_uid)
            if reason is not None:
                logger.warning(
                    "SECURITY sender %s not allowed to message recipient %s (alias=%r) reason=%s",
                    sender_uid,
                    recipient_uid,
                    recipient_alias,
                    reason,
                )
                rejected.append(
                    RejectedRecipient(alias=recipient_alias, uid=recipient_uid, reason=reason)
                )
                continue
            # Read before storing: create_message does not report whether the doc existed.
            conv_existed = (
                messages_store.get_conversation(
                    messages_store.conv_key(sender_uid, recipient_uid)
                )
                is not None
            )
        return SendResult(messages=created, rejected=rejected)

    def _maybe_alert_new_conversation(
    ) -> None:
        """Alerts when this send created a DM and the sender's open policy had no allow edge."""
        sender = users_store.get_user(sender_uid)
        recipient = users_store.get_user(recipient_uid)
        if sender is None or recipient is None:
            return
        if sender.policy.out != "open":
            return
        if allow_store.is_message_allowed(sender_uid, recipient_uid):
            return


    def _candidate_recipients(
        self, sender_uid: str, recipient_alias: str | None, device_id: str | None
    ) -> tuple[list[str], list[RejectedRecipient]]:
        if recipient_alias is not None:
            uid = users_store.get_uid_for_alias(recipient_alias)
            if uid is None:
                logger.warning(
                    "SECURITY unknown recipient alias %r from sender %s", recipient_alias, sender_uid
                )
                return [], [
                    RejectedRecipient(alias=recipient_alias, uid=None, reason="unknown_alias")
                ]
            return [uid], []

        default_uid: str | None = None
        if device_id is not None:
            device = devices_store.get_device(device_id)
            if device is not None:
                default_uid = device.defaultToUid
        if default_uid is not None and _is_external(default_uid):
            default_uid = None
        if default_uid is not None:
            return [default_uid], []
        broadcast = [
            uid for uid in allow_store.allowed_recipients(sender_uid) if not _is_external(uid)
        ]
        return broadcast, []


        """`sms_contact` for external-to-external; an external/person pair needs a smsNumber."""
        sender = users_store.get_user(sender_uid)
        recipient = users_store.get_user(recipient_uid)
        sender_ext = sender is not None and sender.kind == "external"
        recipient_ext = recipient is not None and recipient.kind == "external"
        if sender_ext and recipient_ext:
            return "sms_contact"
        if sender_ext or recipient_ext:
            person = recipient if sender_ext else sender
            ext = sender if sender_ext else recipient
            assert ext is not None
            # A contact belongs to one family and must be live.
            if ext.disabled or person is None or ext.ownerFamilyId != person.familyId:
                return "sms_contact"
            if bridge_row is not None:
                return self._bridge_route_reject(ext, person, bridge_row, outbound=recipient_ext)
            if not person.smsNumber:
                return "no_sms_number"
        return None

    def _bridge_route_reject(
        self, ext: User, person: User, bridge_row: BackendRow, *, outbound: bool
    ) -> RejectReason | None:
        """A bridge external pairs only with its bridge's paired owner; read-only blocks sends."""
        bridge = bridges_store.get(str(bridge_row.config.get("bridgeId") or ""))
        if bridge is None or not bridge.paired or bridge.ownerUid != person.uid:
            return "no_bridge"
        if outbound and (ext.chat or {}).get("canReply") is False:
            return "not_allowed"
        return None


    def _policy_reject_reason(self, sender_uid: str, recipient_uid: str) -> RejectReason | None:
        """The per-recipient gate shared by the DM and group loops."""
        sender = users_store.get_user(sender_uid)
        recipient = users_store.get_user(recipient_uid)
        if sender is None or recipient is None:
            if allow_store.is_message_allowed(sender_uid, recipient_uid):
                return None
            return "not_allowed"
        # In-family persons count as approved; an explicit `message: false` edge beats that.
        has_edge_out = book.edge_or_family(sender, recipient)
        has_edge_in = book.edge_or_family(recipient, sender)
        return policy_module.check(sender, recipient, has_edge_out, has_edge_in)


    def _send_group(
        self,
        *,
        sender_uid: str,
        group: Conversation,
        kind: MessageKind,
        body: str | None,
        loc: dict | None,
        origin_backend_kind: str,
        origin_backend_id: str | None,
        wire_id: str | None,
        ts: int,
        sender_alias: str | None = None,
    ) -> SendResult:
        if sender_uid not in group.uids:
            logger.warning(
                "SECURITY sender %s is not a member of group %s (alias=%r)",
                sender_uid,
                group.convKey,
                group.alias,
            )
            return SendResult(
                messages=[],
                rejected=[
                    RejectedRecipient(alias=group.alias, uid=sender_uid, reason="not_member")
                ],
            )

        if sender_alias is None:
            sender = users_store.get_user(sender_uid)
            sender_alias = sender.alias if sender is not None else sender_uid

        seq = messages_store.allocate_seq()
        # A device-originated send reuses its wire_id as groupMsgId so a redelivery repairs the
        # same copies.
        group_msg_id = wire_id if wire_id is not None else new_id("gm_")

        created: list[Message] = []
        rejected: list[RejectedRecipient] = []
        sender_is_external = _is_external(sender_uid)
        for recipient_uid in sorted(uid for uid in group.uids if uid != sender_uid):
            # SMS contacts never join a relay group; a stale member is skipped. A bridge external
            # may send or receive, and every pair with an external runs the route check first.
            recipient_is_external = _is_external(recipient_uid)
            if (sender_is_external and not sender_is_bridge) or (
            ):
                logger.debug("skipping sms contact in group %s fan-out", group.convKey)
                continue
            if sender_is_external or recipient_is_external:
                if route_reason is not None:
                    logger.warning(
                        "SECURITY sender %s cannot reach bridge peer %s (group=%s) reason=%s",
                        sender_uid,
                        recipient_uid,
                        group.convKey,
                        route_reason,
                    )
                    rejected.append(
                        RejectedRecipient(alias=group.alias, uid=recipient_uid, reason=route_reason)
                    )
                    continue
            reason = self._policy_reject_reason(sender_uid, recipient_uid)
            if reason is not None:
                logger.warning(
                    "SECURITY sender %s not allowed to message group member %s (group=%s) reason=%s",
                    sender_uid,
                    recipient_uid,
                    group.convKey,
                    reason,
                )
                rejected.append(
                    RejectedRecipient(alias=group.alias, uid=recipient_uid, reason=reason)
                )
                continue
        return SendResult(messages=created, rejected=rejected)

    def redeliver_pager(self, msg: Message, device_id: str) -> bool:
        """Re-runs the pager delivery of `msg` for `device_id`; False if there is none."""
        bid = messages_store.find_pager_delivery(msg, device_id)
        if bid is None:
            return False
        delivery = msg.deliveries.get(bid)
        backend_row = backends_store.get_backend(msg.recipientUid, bid)
        if delivery is None or backend_row is None:
            return False
        if self._registry.get("pager") is None:
            return False
        # Via _deliver_one so retries get the same attempts/failed bookkeeping.
        self._deliver_one(msg, bid, delivery, backend_row)
        return True

    def redeliver(self, msg: Message, bid: str) -> bool:
        """Re-runs delivery `bid` of any backend kind; False if the delivery or backend is gone."""
        # Re-read: an earlier attempt may have sent it (a second deliver() would text twice).
        fresh = messages_store.get_message(msg.id)
        delivery = fresh.deliveries.get(bid) if fresh is not None else None
        if fresh is None or delivery is None or delivery.state != "queued":
            return False
        backend_row = backends_store.get_backend(msg.recipientUid, bid)
        if backend_row is None:
            return False
        if self._registry.get(backend_row.kind) is None:
            return False
        self._deliver_one(fresh, bid, delivery, backend_row)
        return True


    def _create_and_deliver(
        self,
        *,
        sender_uid: str,
        recipient_uid: str,
        kind: MessageKind,
        body: str | None,
        loc: dict | None,
        origin_backend_kind: str,
        origin_backend_id: str | None,
        wire_id: str | None,
        ts: int,
        conv_key: str | None = None,
        seq: int | None = None,
        group_msg_id: str | None = None,
        sender_alias: str | None = None,
    ) -> Message | None:
        # Self-loop guard: excludes the origin backend (by id if given, else by kind) only when
        # the recipient is the sender.
        self_loop = recipient_uid == sender_uid
        enabled = [
            b
            for b in backends_store.list_backends(recipient_uid)
            if b.enabled
            and not (
                self_loop
                and (
                    b.id == origin_backend_id
                    if origin_backend_id is not None
                    else b.kind == origin_backend_kind
                )
            )
        ]

        deliveries: dict[str, dict] = {}
        pending_device_ids: list[str] = []
        for b in enabled:
            entry: dict[str, object] = {"kind": b.kind, "state": "queued", "attempts": 0}
            if b.kind == "pager":
                pager_device_id = b.config.get("deviceId")
                if not pager_device_id:
                    logger.warning("pager backend %s has no deviceId configured, skipping", b.id)
                    continue
                entry["externalId"] = pager_device_id
                pending_device_ids.append(pager_device_id)
            deliveries[b.id] = entry

        msg = messages_store.create_message(
            sender_uid=sender_uid,
            recipient_uid=recipient_uid,
            kind=kind,
            ts=ts,
            body=body,
            loc=loc,
            wire_id=wire_id,
            origin_backend_kind=origin_backend_kind,
            origin_backend_id=origin_backend_id,
            deliveries=deliveries,
            pending_device_ids=pending_device_ids,
            conv_key=conv_key,
            seq=seq,
            group_msg_id=group_msg_id,
            sender_alias=sender_alias,
        )
        if msg is None:
            # Already created by an earlier webhook redelivery.
            return None

        backends_by_id = {b.id: b for b in enabled}
        for bid, delivery in msg.deliveries.items():
            backend_row = backends_by_id.get(bid)
            if backend_row is None:
                continue
            self._deliver_one(msg, bid, delivery, backend_row)
        return msg

    def _deliver_one(
        self, msg: Message, bid: str, delivery: Delivery, backend_row: BackendRow
    ) -> None:
        backend = self._registry.get(backend_row.kind)
        if backend is None:
            logger.warning(
                "no backend implementation registered for kind %r (bid=%s, msg=%s)",
                backend_row.kind,
                bid,
                msg.id,
            )
            return
        try:
            result = backend.deliver(msg, delivery, backend_row)
        except Exception:
            # Inline delivery is best-effort: a backend bug must not 500 the sender; it counts
            # as a failed attempt.
            logger.exception(
                "delivery failed for backend kind=%s bid=%s msg=%s", backend_row.kind, bid, msg.id
            )
            result = DeliverResult(
                ok=False, state=delivery.state, error="deliver() raised (see relay logs)"
            )
        # Applies the attempts/failed bookkeeping that adapters do not persist themselves.
        device_id = delivery.externalId if backend_row.kind == "pager" else None
        )
