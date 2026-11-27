"""The routing engine: resolves recipients, gates, hook-filters, stores and delivers messages."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Literal

from app import alerts as alerts_module
from app import bodysplit, book, deliveries
from app import hooks as hooks_module
from app import move as move_module
from app import policy as policy_module
from app.backends.base import Backend, DeliverResult
from app.backends.fed import link_for as fed_backend_link
from app.backends.registry import build_registry
from app.broker import BrokerClient
from app.ids import new_id
from app.store import allow as allow_store
from app.store import backends as backends_store
from app.store import bridges as bridges_store
from app.store import conversations as conversations_store
from app.store import devices as devices_store
from app.store import families as families_store
from app.store import fed_links as fed_links_store
from app.store import fed_state
from app.store import messages as messages_store
from app.store import users as users_store
from app.store.backends import Backend as BackendRow
from app.store.conversations import Conversation
from app.store.messages import Delivery, Message, MessageKind
from app.store.users import User
from app.wire import (
    BODY_MAX_UTF8_BYTES,
    LEGACY_BODY_MAX_CODEPOINTS,
    LEGACY_BODY_MAX_UTF8_BYTES,
)

logger = logging.getLogger("relay.routing")

_UNSET: Any = object()

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
    "hook_blocked",
    "no_peer",
    "fed_reauth",
    "fed_off",
    "family_frozen",
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
    # A blocking filter's `notifySender` text, shown to the sender.
    notify: str | None = None

    @property
    def ok(self) -> bool:
        return bool(self.messages) and not self.rejected


def _part_id(wire_id: str | None, index: int) -> str | None:
    """Part 0 keeps the wire id, part `i` is `<wire>~<i>`; no wire id stays none."""
    return wire_id if index == 0 or wire_id is None else f"{wire_id}~{index}"


@dataclass
class _Staged:
    """What the hook stage hands the storage stage."""

    body: str | None
    blocked: bool = False
    notify: str | None = None
    ctx: hooks_module.MessageCtx | None = None
    annotations: dict[str, dict] = field(default_factory=dict)
    ref: str | None = None


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
        origin_device_id: str | None = None,
    ) -> SendResult:
        """`origin_device_id` limits the delivery receipt to that pager; `sender_alias` overrides
        the group copy's `senderAlias`."""
        ts = ts if ts is not None else int(time.time())
        if families_store.user_family_frozen(sender_uid):
            raise families_store.FamilyFrozen(sender_uid)

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
                    origin_device_id=origin_device_id,
                )

        candidates, rejected = self._candidate_recipients(sender_uid, recipient_alias, device_id)

        # Stage 1: the per-recipient gate. Nothing is stored yet.
        admitted: list[str] = []
        for recipient_uid in candidates:
            if families_store.user_family_frozen(recipient_uid):
                rejected.append(
                    RejectedRecipient(
                        alias=recipient_alias, uid=recipient_uid, reason="family_frozen"
                    )
                )
                continue
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
            admitted.append(recipient_uid)

        # Stage 2: the hook filter chain, once for the whole message.
        staged = self._filter_stage(
            sender_uid=sender_uid,
            admitted=admitted,
            kind=kind,
            body=body,
            origin_backend_kind=origin_backend_kind,
            wire_id=wire_id,
            ts=ts,
            conv_key=(
                messages_store.conv_key(sender_uid, admitted[0]) if admitted else None
            ),
            group=None,
        )
        if staged.blocked:
            return self._blocked_result(staged, recipient_alias, admitted, rejected)
        body = staged.body

        # Stage 3: store and deliver, per recipient, with the final body.
        created: list[Message] = []
        for recipient_uid in admitted:
            # Read before storing: create_message does not report whether the doc existed.
            conv_existed = (
                messages_store.get_conversation(
                    messages_store.conv_key(sender_uid, recipient_uid)
                )
                is not None
            )
            # One message per part, sized for this recipient's pager.
            parts = self._split_for(kind, body, [recipient_uid])
            first: Message | None = None
            for index, part in enumerate(parts):
                msg = self._create_and_deliver(
                    sender_uid=sender_uid,
                    recipient_uid=recipient_uid,
                    kind=kind,
                    body=part,
                    loc=loc,
                    origin_backend_kind=origin_backend_kind,
                    origin_backend_id=origin_backend_id,
                    wire_id=_part_id(wire_id, index),
                    ts=ts,
                    sender_alias=sender_alias,
                    origin_device_id=origin_device_id,
                    hooks=staged.annotations,
                    hook_ref=staged.ref,
                )
                if msg is not None:
                    created.append(msg)
                    first = first or msg
            if first is not None and not conv_existed:
                self._maybe_alert_new_conversation(sender_uid, recipient_uid, first.convKey, body)
        self._after_store(staged, created)
        return SendResult(messages=created, rejected=rejected)


    def _filter_stage(
        self,
        *,
        sender_uid: str,
        admitted: list[str],
        kind: MessageKind,
        body: str | None,
        origin_backend_kind: str,
        wire_id: str | None,
        ts: int,
        conv_key: str | None,
        group: Conversation | None,
    ) -> _Staged:
        """Runs the hook chain between the policy gate and storage."""
        staged = _Staged(body=body)
        if kind != "text" or not body or not admitted or conv_key is None:
            return staged
        hooks = hooks_module.active_hooks()
        if not hooks:
            return staged
        if wire_id is not None and all(
            messages_store.wire_id_exists(wire_id, uid) for uid in admitted
        ):
            # A QoS-1 redelivery already stored for everyone: skip the hooks.
            return staged
        ctx = hooks_module.build_ctx(
            sender_uid=sender_uid,
            recipient_uids=admitted,
            conv_key=conv_key,
            conv_kind="group" if group is not None else "dm",
            conv_name=group.name if group is not None else None,
            origin_backend_kind=origin_backend_kind,
            wire_id=wire_id,
            ts=ts,
        )
        if ctx is None:
            return staged
        covering = [h for h in hooks if h.covers(ctx.family_ids)]
        if not covering:
            return staged
        chain = hooks_module.run_filters(covering, ctx, body)
        staged.ctx = ctx
        staged.body = chain.body
        staged.blocked = chain.blocked
        staged.notify = chain.notify
        staged.annotations = chain.annotations
        staged.ref = ctx.ref
        return staged


    def _split_for(self, kind: MessageKind, body: str | None, recipient_uids: list[str]) -> list[str | None]:
        """`[body]` unless a text body exceeds the smallest recipient cap."""
        if kind != "text" or not body:
            return [body]
        if len(body) <= LEGACY_BODY_MAX_CODEPOINTS and len(body.encode("utf-8")) <= (
            LEGACY_BODY_MAX_UTF8_BYTES
        ):
            return [body]
        max_bytes = BODY_MAX_UTF8_BYTES
        max_cp: int | None = None
        for uid in recipient_uids:
            b, cp = self._recipient_cap(uid)
            max_bytes = min(max_bytes, b)
            if cp is not None:
                max_cp = cp if max_cp is None else min(max_cp, cp)
        return list(bodysplit.split_body(body, max_bytes=max_bytes, max_codepoints=max_cp))

    @staticmethod
    def _recipient_cap(recipient_uid: str) -> tuple[int, int | None]:
        """`(max bytes, max code points | None)`: minimum over the recipient's live pagers."""
        max_bytes = BODY_MAX_UTF8_BYTES
        max_cp: int | None = None
        for b in backends_store.list_backends(recipient_uid):
            if b.kind != "pager" or not b.enabled:
                continue
            device = devices_store.get_device(str(b.config.get("deviceId") or ""))
            if device is None or device.revokedAt is not None:
                continue
            mb = device.status.mb
            if mb:
                max_bytes = min(max_bytes, mb)
            else:
                max_bytes = min(max_bytes, LEGACY_BODY_MAX_UTF8_BYTES)
                max_cp = LEGACY_BODY_MAX_CODEPOINTS
        return max_bytes, max_cp

    @staticmethod
    def _after_store(staged: _Staged, created: list[Message]) -> None:
        """Tells hook processors about the stored message."""
        if staged.ctx is None or not created or staged.body is None:
            return
        hooks_module.dispatch_processors(
            hooks_module.active_hooks(),
            staged.ctx,
            body=staged.body,
            message_ids=[m.id for m in created],
        )

    @staticmethod
    def _blocked_result(
        staged: _Staged,
        alias: str | None,
        admitted: list[str],
        rejected: list[RejectedRecipient],
    ) -> SendResult:
        """Stored nowhere; every admitted recipient is reported `hook_blocked`."""
        return SendResult(
            messages=[],
            rejected=[
                *rejected,
                *(RejectedRecipient(alias=alias, uid=uid, reason="hook_blocked") for uid in admitted),
            ],
            notify=staged.notify,
        )

    def _maybe_alert_new_conversation(
        self, sender_uid: str, recipient_uid: str, conv_key: str, body: str | None = None
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
        alerts_module.new_conversation(sender, recipient, conv_key, body)


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


    def _sms_route_reject(
        self, sender_uid: str, recipient_uid: str, *, bridge_row: Any = _UNSET
    ) -> RejectReason | None:
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
            if ext.fed:
                # A federated contact has no number or bridge; its link must be active.
                return self._fed_route_reject(ext)
            if bridge_row is _UNSET:
                bridge_row = backends_store.get_backend(ext.uid, "bridge")
            if bridge_row is not None:
                return self._bridge_route_reject(ext, person, bridge_row, outbound=recipient_ext)
            if not person.smsNumber:
                return "no_sms_number"
        return None

    @staticmethod
    def _fed_route_reject(ext: User) -> RejectReason | None:
        if not fed_state.family_enabled(ext.ownerFamilyId):
            return "fed_off"
        link = fed_links_store.get_for(
            ext.ownerFamilyId, str((ext.fed or {}).get("remoteFedId") or "")
        )
        if link is not None and link.state == "reauth":
            return "fed_reauth"
        if link is None or link.state != "active":
            return "no_peer"
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
        origin_device_id: str | None = None,
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
        admitted: list[str] = []
        sender_is_external = _is_external(sender_uid)
        # One `bridge` backend read per external, reused for the skip decision and route check.
        sender_bridge_row = (
            backends_store.get_backend(sender_uid, "bridge") if sender_is_external else None
        )
        sender_is_bridge = sender_bridge_row is not None
        for recipient_uid in sorted(uid for uid in group.uids if uid != sender_uid):
            # SMS contacts never join a relay group; a stale member is skipped. A bridge external
            # may send or receive, and every pair with an external runs the route check first.
            if families_store.user_family_frozen(recipient_uid):
                rejected.append(
                    RejectedRecipient(
                        alias=group.alias, uid=recipient_uid, reason="family_frozen"
                    )
                )
                continue
            recipient_is_external = _is_external(recipient_uid)
            recipient_bridge_row = (
                backends_store.get_backend(recipient_uid, "bridge")
                if recipient_is_external
                else None
            )
            if (sender_is_external and not sender_is_bridge) or (
                recipient_is_external and recipient_bridge_row is None
            ):
                logger.debug("skipping sms contact in group %s fan-out", group.convKey)
                continue
            if sender_is_external or recipient_is_external:
                route_reason = self._sms_route_reject(
                    sender_uid,
                    recipient_uid,
                    bridge_row=sender_bridge_row if sender_is_external else recipient_bridge_row,
                )
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
            admitted.append(recipient_uid)

        # The hook filter chain, once for the whole group message.
        staged = self._filter_stage(
            sender_uid=sender_uid,
            admitted=admitted,
            kind=kind,
            body=body,
            origin_backend_kind=origin_backend_kind,
            wire_id=group_msg_id,
            ts=ts,
            conv_key=group.convKey,
            group=group,
        )
        if staged.blocked:
            return self._blocked_result(staged, group.alias, admitted, rejected)
        body = staged.body

        # Every member gets the same parts: the cap is the smallest across them.
        parts = self._split_for(kind, body, admitted)
        for index, part in enumerate(parts):
            part_id = _part_id(group_msg_id, index)
            part_seq = seq if index == 0 else messages_store.allocate_seq()
            for recipient_uid in admitted:
                msg = self._create_and_deliver(
                    sender_uid=sender_uid,
                    recipient_uid=recipient_uid,
                    kind=kind,
                    body=part,
                    loc=loc,
                    origin_backend_kind=origin_backend_kind,
                    origin_backend_id=origin_backend_id,
                    wire_id=part_id,
                    ts=ts,
                    conv_key=group.convKey,
                    seq=part_seq,
                    group_msg_id=part_id,
                    sender_alias=sender_alias,
                    origin_device_id=origin_device_id,
                    hooks=staged.annotations,
                    hook_ref=staged.ref,
                )
                if msg is not None:
                    created.append(msg)
        self._after_store(staged, created)
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
        hooks: dict[str, dict] | None = None,
        hook_ref: str | None = None,
        origin_device_id: str | None = None,
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
            elif b.kind == "fed" and b.config.get("remoteFedId"):
                # the other family's name and server, so the web chip can name them
                fed_link = fed_backend_link(recipient_uid, str(b.config["remoteFedId"]))
                if fed_link is not None:
                    entry["server"] = fed_link.remote.name
                    entry["serverHost"] = fed_link.remote.where
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
            origin_device_id=origin_device_id,
            hooks=hooks,
            hook_ref=hook_ref,
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
        if backend_row.kind == "pager" and move_module.delivery_held(
            backend_row.config.get("deviceId")
        ):
            # imported pager that has not arrived: no publish, no attempt
            logger.info("delivery of %s to %s held: pager has not arrived", msg.id, bid)
            return
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
        deliveries.record_attempt(
            msg.id,
            bid,
            ok=result.ok,
            error=result.error,
            device_id=device_id,
            kind=backend_row.kind,
        )
