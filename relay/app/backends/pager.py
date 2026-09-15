"""`pager` backend: the MQTT gateway over the broker's REST publish API.

from __future__ import annotations

import logging

from pydantic import BaseModel, ConfigDict

from app.backends.base import DeliverResult, LinkStep
from app.broker import BrokerClient
from app.store import users as users_store
from app.store.backends import Backend as BackendRow
from app.store.messages import Delivery, Message
from app.store.users import User

logger = logging.getLogger("relay.backends.pager")

SYSTEM_ALIAS = "system"


class PagerConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    deviceId: str


class PagerBackend:
    kind = "pager"
    config_schema = PagerConfig

    def __init__(self, broker: BrokerClient) -> None:
        self._broker = broker

    def deliver(self, msg: Message, delivery: Delivery, backend: BackendRow) -> DeliverResult:
        device_id = backend.config.get("deviceId")
        if not device_id:
            logger.warning("pager backend %s has no deviceId configured", backend.id)
            return DeliverResult(ok=False, state="failed", error="pager backend missing deviceId")

        down_kind = "loc_req" if msg.kind == "loc_req" else "msg"
            msg_id=msg.id,
            ts=msg.ts,
            kind=down_kind,
        )
        if ok:
            return DeliverResult(ok=True, state="sent", external_id=device_id)
        return DeliverResult(ok=False, state="queued", error="broker publish failed")

    def start_link(self, user: User, backend: BackendRow) -> LinkStep | None:
        return None

    def complete_link(self, backend: BackendRow, proof: str) -> bool:
        return False

    def render_state(self, delivery: Delivery) -> str:
        return delivery.state

    @staticmethod
    def _sender_alias(sender_uid: str) -> str:
        user = users_store.get_user(sender_uid)
        return user.alias if user is not None else SYSTEM_ALIAS
