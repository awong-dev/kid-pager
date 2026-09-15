"""`pager` backend: the MQTT gateway over the broker's REST publish API.
The wire `id` is the message id, so republishes dedup on the device (docs/PROTOCOL.md §5.3)."""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, ConfigDict

from app.backends.base import DeliverResult, LinkStep
from app.broker import BrokerClient
from app.store import users as users_store
from app.store.backends import Backend as BackendRow
from app.store.messages import Delivery, Message
from app.store.users import User

logger = logging.getLogger("relay.backends.pager")

SYSTEM_ALIAS = "system"


    """The down envelope's fields (docs/PROTOCOL.md §3.1/§3.2); `sndr` only on a group page."""
    obj: dict[str, Any] = {"v": 1, "id": msg_id, "ts": ts}
    if kind != "msg":
        obj["kind"] = kind
    obj["from"] = from_
    if kind != "loc_req":
        obj["body"] = body
    obj["ack"] = None
    return obj


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
        obj = _build_down_obj(
            msg_id=msg.id,
            ts=msg.ts,
            kind=down_kind,
            body=msg.body,
        )
        ok = self._broker.publish_down(device_id, obj)
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
