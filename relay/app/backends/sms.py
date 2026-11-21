"""`sms` backend: SMS exists only through paired bridge phones.
No sender number fails `no_sms_number`; no usable bridge fails `no_bridge`; neither is retried."""

from __future__ import annotations

import logging

from pydantic import BaseModel, ConfigDict

from app import deliveries
from app.backends.base import DeliverResult, LinkStep
from app.backends.bridge import apply_outbox_state
from app.sms_text import redact_phone, voice_link
from app.store import bridge_outbox
from app.store import bridges as bridges_store
from app.store import users as users_store
from app.store.backends import Backend as BackendRow
from app.store.messages import Delivery, Message
from app.store.users import User

logger = logging.getLogger("relay.backends.sms")

LOC_PREVIEW = "location"


class SmsConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    phone: str


def _render_body(msg: Message) -> str:
    """`kind='loc'` has no `body`, so a short fixed preview stands in."""
    if msg.body:
        return msg.body
    if msg.kind == "loc" and msg.loc is not None:
        return LOC_PREVIEW
    return "(no content)"


class SmsBackend:
    kind = "sms"
    config_schema = SmsConfig

    def deliver(self, msg: Message, delivery: Delivery, backend: BackendRow) -> DeliverResult:
        phone = backend.config.get("phone")
        if not phone:
            logger.warning("sms backend %s has no phone configured", backend.id)
            deliveries.mark_failed(msg.id, backend.id, kind=self.kind)
            return DeliverResult(ok=False, state="failed", error="sms backend missing phone")

        sender = users_store.get_user(msg.senderUid)
        from_number = sender.smsNumber if sender is not None else None
        if not from_number:
            logger.info(
                "sms out to=%s from=- sid=- status=failed code=no_sms_number",
                redact_phone(phone),
            )
            deliveries.mark_failed(msg.id, backend.id, kind=self.kind)
            return DeliverResult(ok=False, state="failed", error="no_sms_number")

        # The sender's number must be a paired bridge's SIM or Voice number and the phone
        # must be able to send something right now; otherwise the send fails, never retried.
        bridge = bridges_store.get_by_sms_number(from_number, prefer_owner=sender.uid)
        if bridge is None or not (bridge.caps.sms or bridge.caps.gvoice or bridge.caps.whatsapp):
            logger.info(
                "sms out to=%s from=%s sid=- status=failed code=no_bridge",
                redact_phone(phone),
                redact_phone(from_number),
            )
            deliveries.mark_failed(msg.id, backend.id, error="no_bridge", kind=self.kind)
            return DeliverResult(ok=False, state="failed", error="no_bridge")
        return self._deliver_via_bridge(msg, backend, bridge, phone, from_number)

    def _deliver_via_bridge(
        self,
        msg: Message,
        backend: BackendRow,
        bridge: bridges_store.Bridge,
        phone: str,
        from_number: str,
    ) -> DeliverResult:
        """Channel: `config.via` if set, else `gvoice` for the Voice number, else `sms`."""
        via = (backend.config.get("via") or {}).get(msg.senderUid)
        if via not in ("sms", "gvoice"):
            via = "gvoice" if from_number == bridge.voiceNumber != bridge.simNumber else "sms"
        order = {
            "gvoice": ("gvoice", "sms"),
            "sms": ("sms", "gvoice"),
        }[via]
        chosen = next((v for v in order if getattr(bridge.caps, v)), None)
        if chosen is None:
            logger.info(
                "sms out to=%s from=%s sid=- status=failed code=no_bridge",
                redact_phone(phone),
                redact_phone(from_number),
            )
            deliveries.mark_failed(msg.id, backend.id, error="no_bridge", kind=self.kind)
            return DeliverResult(ok=False, state="failed", error="no_bridge")
        via = chosen
        to: dict[str, str] = {"phone": phone}
        if via == "gvoice":
            # `voiceConv` is the per-member Voice thread id.
            conv = (backend.config.get("voiceConv") or {}).get(msg.senderUid)
            if conv:
                to["conversationId"] = conv
            to["link"] = voice_link(phone)
        item = bridge_outbox.enqueue_send(
            bridge, msg.id, backend.id, source=via, to=to, text=_render_body(msg)
        )
        logger.info(
            "sms out to=%s from=%s sid=%s status=%s code=bridge",
            redact_phone(phone),
            redact_phone(from_number),
            item.id,
            "queued" if item.state == "pending" else item.state,
        )
        return apply_outbox_state(msg, backend.id, item)

    def start_link(self, user: User, backend: BackendRow) -> LinkStep | None:
        return None

    def complete_link(self, backend: BackendRow, proof: str) -> bool:
        return False

    def render_state(self, delivery: Delivery) -> str:
        return "sent by SMS" if delivery.state == "sent" else delivery.state
