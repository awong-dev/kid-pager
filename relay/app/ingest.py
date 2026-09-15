"""Business logic for inbound device traffic posted to `/webhooks/mqtt`: signature verification, ack
state machine, dedup and online-edge republish (docs/PROTOCOL.md §4, §5, §14.4)."""

from __future__ import annotations

import hashlib
import logging
import time

from app.broker import BrokerClient
from app.routing import Routing
from app.store import devices as devices_store
from app.store import messages as messages_store
from app.wire import (
    LocEnvelope,
    StatusEnvelope,
    UpEnvelope,
    resolve_ts,
)

logger = logging.getLogger("relay.ingest")

# PROTOCOL.md §4.2 case 3: the one exception to "never auto-reply on MQTT".
UNKNOWN_RECIPIENT_BODY = "unknown recipient"


def _device_id_from_topic(topic: str, expected_suffix: str) -> str | None:
    parts = topic.split("/")
    if len(parts) != 3 or parts[0] != "pager" or parts[2] != expected_suffix:
        return None
    return parts[1]


class Ingest:
    """Owns the relay's reaction to device traffic; the webhook router calls it once per event.

    def __init__(self, broker: BrokerClient, routing: Routing | None = None) -> None:
        self._broker = broker
        self._routing = routing if routing is not None else Routing(broker)


    def handle_up(self, topic: str, payload: bytes) -> None:
        device_id = _device_id_from_topic(topic, "up")
        if device_id is None:
            logger.warning("up webhook for unrecognised topic %s dropped", topic)
            return
            return
        try:
            env = UpEnvelope.model_validate(data)
        except Exception as exc:  # noqa: BLE001 -- pydantic.ValidationError, narrowly caught above
            return

        if env.is_ack:
            self._handle_ack(device_id, env)
        else:

    def _handle_ack(self, device_id: str, env: UpEnvelope) -> None:
        assert env.ack is not None and env.ack in ("shown", "read")
        ack_ts = resolve_ts(env.ts)

        # A down message's wire `id` is its `messages/{id}` doc id, except a `book`/`cfg` id, which
        msg = messages_store.get_message(env.id)
        if msg is None:
            # docs/PROTOCOL.md §4.1 rule 3: unknown id -> log + drop, never create a row.
            logger.info("ack for unknown message id=%s from device=%s dropped", env.id, device_id)
            return
        self._handle_v2_ack(device_id, msg, env.ack, ack_ts)

    def _handle_v2_ack(
        self,
        device_id: str,
        msg: messages_store.Message,
        ack: Literal["shown", "read"],
        ack_ts: int,
    ) -> None:
        bid = messages_store.find_pager_delivery(msg, device_id)
        if bid is None:
            # docs/PROTOCOL.md §4.1 rule 4: the message has no pager delivery for this device.
            logger.warning(
                "SECURITY wrong-device ack: pager/%s/up acked message %s with no matching "
                "pager delivery",
                device_id,
                msg.id,
            )
            return
        result = messages_store.apply_delivery_ack(msg.id, bid, device_id, ack, ack_ts)
        if result == "noop":
            logger.debug("idempotent ack '%s' for message %s backend %s", ack, msg.id, bid)

        if device is None:
            # Not a registered device: nowhere to route it, dropped like an unknown recipient.
            logger.warning("up message %s from unregistered device %s dropped", env.id, device_id)
            return
        if device.revokedAt is not None:
            # A revoked device is not allowed to inject anything.
            logger.warning(
                "SECURITY up message %s from revoked device %s dropped", env.id, device_id
            )
            return
        self._handle_v2_up_message(device, env)

    def _handle_v2_up_message(self, device: devices_store.Device, env: UpEnvelope) -> None:
        # `routing.send()` does the allow-list check, the transactional dedup by wireId (covers
        # QoS-1 redelivery) and the fan-out to the recipient's enabled backends.
        result = self._routing.send(
            sender_uid=device.ownerUid,
            recipient_alias=env.to,
            kind="text",
            body=env.body,
            origin_backend_kind="pager",
            wire_id=env.id,
            device_id=device.id,
            ts=resolve_ts(env.ts),
        )
            # docs/PROTOCOL.md §4.2 case 3: an unknown or disallowed explicit `to` gets exactly one
            # `system` down reply, never stored as a message, one per offending up message.

    def _send_system_reply(self, device_id: str, body: str, *, cause_id: str) -> None:
        # The reply's `id` is derived from the offending up message's id (docs/PROTOCOL.md §4.2);
        # a fresh id would re-alert on a QoS 1 redelivery.


    def handle_status(self, topic: str, payload: bytes) -> None:
        device_id = _device_id_from_topic(topic, "status")
        if device_id is None:
            logger.warning("status webhook for unrecognised topic %s dropped", topic)
            return
            return
        try:
            env = StatusEnvelope.model_validate(data)
        except Exception as exc:  # noqa: BLE001 -- pydantic.ValidationError, narrowly caught above
            return

        previous_status = device.status
        resolved_ts = resolve_ts(env.ts) if env.ts is not None else None
        devices_store.update_status(
            device_id,
            state=env.state,
            mode=env.mode,
            battMv=env.batt_mv,
            rssi=env.rssi,
            session=env.session,
            ts=resolved_ts,
            fw=env.fw,
            locPeriodS=env.loc_period_s,
            locMinS=env.loc_min_s,
        )

        if env.state != "online":
            # docs/PROTOCOL.md §5.3: bare offline (retained or LWT) -> mark offline only.
            return

        offline_to_online = previous_status.state == "offline"
        if session_changed or offline_to_online:
            self._republish_unacked(device_id)

    def _republish_unacked(self, device_id: str) -> None:
        """The online-edge re-publish (docs/PROTOCOL.md §5.3): pending messages from `pendingDeviceIds`
        one each, which do not count against the cap."""
        for msg in messages_store.list_pending_for_device(device_id):
            if msg.kind == "loc_req":
                continue
            logger.info("re-publishing unacked message %s to device %s", msg.id, device_id)
            self._routing.redeliver_pager(msg, device_id)


    def handle_loc(self, topic: str, payload: bytes) -> None:
        """Validate per docs/PROTOCOL.md §13.2, then hand off to `app.location.ingest_loc`, which never
        originates a delivery: it only resolves an existing `loc_req` delivery to `fulfilled`."""
        device_id = _device_id_from_topic(topic, "loc")
        if device_id is None:
            logger.warning("loc webhook for unrecognised topic %s dropped", topic)
            return
            return
        try:
            env = LocEnvelope.model_validate(data)
        except Exception as exc:  # noqa: BLE001 -- pydantic.ValidationError, narrowly caught above
            return
        location.ingest_loc(device_id, env)
