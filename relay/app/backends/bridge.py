"""`bridge` backend: a subscribed Google Chat / Google Voice conversation.
`deliver()` enqueues on the bridge phone's outbox; the phone's ack moves it to `sent`/`failed`."""

from __future__ import annotations

import logging

from pydantic import BaseModel, ConfigDict

from app import deliveries
from app.backends.base import DeliverResult, LinkStep
from app.store import bridge_conversations, bridge_outbox
from app.store import bridges as bridges_store
from app.store import messages as messages_store
from app.store.backends import Backend as BackendRow
from app.store.messages import Delivery, Message
from app.store.users import User

logger = logging.getLogger("relay.backends.bridge")


class BridgeConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    bridgeId: str
    source: str = "gchat"
    conversationId: str | None = None
    link: str | None = None


def apply_outbox_state(
    msg: Message, backend_id: str, item: bridge_outbox.OutboxItem
) -> DeliverResult:
    """The outbox row's state decides the delivery's."""
    messages_store.set_delivery_external_id(msg.id, backend_id, item.id)
    if item.state == "sent":
        deliveries.mark_sent(msg.id, backend_id, kind="bridge")
        return DeliverResult(ok=True, state="sent", external_id=item.id)
    if item.state == "failed":
        deliveries.mark_failed(msg.id, backend_id, error=item.reason, kind="bridge")
        return DeliverResult(ok=False, state="failed", external_id=item.id, error=item.reason)
    return DeliverResult(ok=True, state="queued", external_id=item.id)


class BridgeBackend:
    kind = "bridge"
    config_schema = BridgeConfig

    def deliver(self, msg: Message, delivery: Delivery, backend: BackendRow) -> DeliverResult:
        config = BridgeConfig.model_validate(backend.config)
        bridge = bridges_store.get(config.bridgeId)
        if bridge is None or not bridge.paired:
            deliveries.mark_failed(msg.id, backend.id, error="no_bridge", kind=self.kind)
            return DeliverResult(ok=False, state="failed", error="no_bridge")
        row = (
            bridge_conversations.get(bridge.id, config.conversationId)
            if config.conversationId
            else None
        )
        if row is not None and row.status == "paused":
            deliveries.mark_failed(msg.id, backend.id, error="paused", kind=self.kind)
            return DeliverResult(ok=False, state="failed", error="paused")
        if msg.kind != "text" or not msg.body:
            deliveries.mark_failed(msg.id, backend.id, error="unsupported", kind=self.kind)
            return DeliverResult(ok=False, state="failed", error="unsupported")
        to: dict[str, str | None] = {"conversationId": config.conversationId, "link": config.link}
        if config.conversationId:
            # Tier 2 finds a conversation by its title (all chat sources).
            title = row.title if row is not None else None
            if title:
                to["title"] = title
        item = bridge_outbox.enqueue_send(
            bridge,
            msg.id,
            backend.id,
            source=config.source,
            to=to,
            text=msg.body,
            reply_hint=config.conversationId,
        )
        logger.info(
            "bridge enqueue bridge=%s ob=%s src=%s state=%s", bridge.id, item.id, config.source, item.state
        )
        return apply_outbox_state(msg, backend.id, item)

    def start_link(self, user: User, backend: BackendRow) -> LinkStep | None:
        return None

    def complete_link(self, backend: BackendRow, proof: str) -> bool:
        return False

    def render_state(self, delivery: Delivery) -> str:
        if delivery.state == "queued":
            return "waiting for the phone"
        if delivery.state == "sent":
            return "sent on Google Chat"
        return delivery.state
