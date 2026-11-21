"""`fed` backend: a person on a peer relay.
`deliver()` writes `fedOutbox/ob_<msgId>` (create-only) and leaves the delivery `queued`."""

from __future__ import annotations

import logging

from pydantic import BaseModel, ConfigDict

from app import deliveries
from app.backends.base import DeliverResult, LinkStep
from app.fed import sender
from app.store import fed_links, fed_outbox
from app.store import users as users_store
from app.store.backends import Backend as BackendRow
from app.store.messages import Delivery, Message
from app.store.users import User

logger = logging.getLogger("relay.backends.fed")


class FedConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    remoteFedId: str
    rid: str


def outbox_id(msg_id: str) -> str:
    return f"ob_{msg_id}"


def link_for(external_uid: str, remote_fed_id: str) -> fed_links.FedLink | None:
    """The link the external `external_uid` is reached through."""
    external = users_store.get_user(external_uid)
    if external is None or not external.ownerFamilyId:
        return None
    return fed_links.get_for(external.ownerFamilyId, remote_fed_id)


class FedBackend:
    kind = "fed"
    config_schema = FedConfig

    def _fail(self, msg: Message, backend: BackendRow, code: str) -> DeliverResult:
        logger.info("fed out msg=%s failed code=%s", msg.id, code)
        deliveries.mark_failed(msg.id, backend.id, error=code, kind=self.kind)
        return DeliverResult(ok=False, state="failed", error=code)

    def deliver(self, msg: Message, delivery: Delivery, backend: BackendRow) -> DeliverResult:
        config = FedConfig.model_validate(backend.config)
        link = link_for(msg.recipientUid, config.remoteFedId)
        if link is None or link.state != "active":
            return self._fail(msg, backend, "unpeered")
        if msg.kind != "text" or not msg.body:
            return self._fail(msg, backend, "unsupported")
        row = fed_outbox.enqueue(
            outbox_id(msg.id),
            link.id,
            link.familyId,
            {
                "type": "msg",
                "id": msg.id,
                "from": msg.senderUid,
                "to": config.rid,
                "body": msg.body,
                "ts": msg.ts,
            },
            msg_id=msg.id,
        )
        logger.info("fed enqueue link=%s ob=%s state=%s", link.id, row.id, row.state)
        if row.state == "pending":
            sender.kick(link.id)
        return DeliverResult(ok=True, state="queued", external_id=row.id)

    def start_link(self, user: User, backend: BackendRow) -> LinkStep | None:
        return None

    def complete_link(self, backend: BackendRow, proof: str) -> bool:
        return False

    def render_state(self, delivery: Delivery) -> str:
        if delivery.state == "queued":
            return "waiting for the other family's server"
        return "sent to the other family" if delivery.state == "sent" else delivery.state
