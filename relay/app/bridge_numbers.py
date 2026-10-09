"""Applying a bridge's accepted numbers to its owner."""

from __future__ import annotations

import logging

from app import book
from app.broker import BrokerClient
from app.store import bridges as bridges_store
from app.store import users as users_store

logger = logging.getLogger("relay.bridge")


def desired_sms_number(bridge: bridges_store.Bridge) -> str | None:
    return bridge.simNumber or bridge.voiceNumber


    """`caps.sms`: SIM present and phone can send; `gvoice`: Voice number; `whatsapp`: no number."""


def apply_numbers(bridge: bridges_store.Bridge, broker: BrokerClient) -> bool:
    """Sets the owner's `smsNumber` to the bridge's and re-derives contacts; True on success."""
    owner = users_store.get_user(bridge.ownerUid)
    if owner is None:
        return False
    number = desired_sms_number(bridge)
    if number is None:
        return True
    if owner.smsNumber != number:
        # A person may own several bridges only if each has a different number: a
        # second bridge never takes the number over from another the owner uses.
        if current is not None and current.id != bridge.id and current.ownerUid == owner.uid:
            bridges_store.set_error(bridge.id, f"@{owner.alias} already uses another bridge phone")
            logger.info("bridge number refused bridge=%s other=%s", bridge.id, current.id)
            return False
        try:
            users_store.set_sms_number(owner.uid, number)
        except users_store.SmsNumberTaken as exc:
            holder = users_store.get_user(exc.holder_uid)
            label = f"@{holder.alias}" if holder else exc.holder_uid
            bridges_store.set_error(bridge.id, f"sim number belongs to {label}")
            logger.info("bridge sim taken bridge=%s holder=%s", bridge.id, exc.holder_uid)
            return False
    book.rederive_sms_contacts(owner.uid, broker)
    return True


def release_numbers(bridge: bridges_store.Bridge, broker: BrokerClient) -> None:
    owner = users_store.get_user(bridge.ownerUid)
    if owner is None:
        return
    if owner.smsNumber and owner.smsNumber in {bridge.simNumber, bridge.voiceNumber}:
        users_store.set_sms_number(owner.uid, None)
        book.bump_and_push({owner.uid}, broker, reason="sms_number")
    book.rederive_sms_contacts(owner.uid, broker)


def rederive(owner_uid: str, broker: BrokerClient) -> None:
    book.rederive_sms_contacts(owner_uid, broker)
