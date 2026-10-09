
from __future__ import annotations

import logging
from collections.abc import Callable, Sequence

from app import alerts as alerts_module
from app import policy as policy_module
from app.routing import Routing
from app.store import backends as backends_store
from app.store import externals as externals_store
from app.store import families as families_store
from app.store import held_sms as held_sms_store
from app.store import messages as messages_store
from app.store.users import User

logger = logging.getLogger("relay.inbound_text")


def placeholder_for(attachments: Sequence[str]) -> str:
    """Empty text with attachments: `[photo]` for exactly one image, else `[attachment]`."""
    return "[photo]" if list(attachments) == ["image"] else "[attachment]"


    contact_uid: str, bid: str, member_uid: str, via: str, voice_conv: str | None
) -> None:
    """The channel a member last used with this external, kept per member on the external's `sms`
    backend row (the contact is family-wide)."""


def handle_text(
    target: User,
    from_number: str,
    raw_body: str,
    sid: str,
    routing: Routing,
    *,
    reply: Callable[[str], object],
    attachments: Sequence[str] = (),
    via: str | None = None,
    voice_conv: str | None = None,
) -> str:
    """Blocked, duplicate, empty, known contact -> deliver, else held. Returns the outcome label.
    family_id = target.familyId
    assert family_id is not None
    family = families_store.get_family(family_id)
    if family is not None and from_number in family.blockedNumbers:
        return "blocked"

    if held_sms_store.exists(sid) or messages_store.wire_id_exists(sid, target.uid):
        return "duplicate"

    # `raw_body` is kept as received (held rows show parents the original); `body` is what may
    # reach a pager (docs/PROTOCOL.md §3.1 control characters).
    raw_body = (raw_body or "").strip()
    if not body:
        if not attachments:
            return "dropped_empty"
        raw_body = body = placeholder_for(attachments)

    contact = externals_store.get_family_contact(family_id, from_number)
    if (
        contact is not None
        and not contact.disabled
        and policy_module.check(contact, target, False, book.edge_or_family(target, contact))
        is None
    ):
        bid = externals_store.ensure_sms_backend(contact)
        result = routing.send(
            sender_uid=contact.uid,
            recipient_alias=target.alias,
            kind="text",
            body=body,
            origin_backend_kind="sms",
            origin_backend_id=bid,
            wire_id=sid,
        )
        if result.rejected:
            return f"rejected_{result.rejected[0].reason}"
        if not result.messages:
            return "duplicate"
        if via is not None:
        return "delivered"

    # Held: no contact, no edge, or the member's numbers rule is `none`.
    if held_sms_store.count_held(family_id, from_number, target.uid) >= held_sms_store.HELD_CAP:
        return "held_cap"
    if not held_sms_store.create(
    ):
        return "duplicate"
    alert_id = alerts_module.sms_held_upsert(family_id, target, from_number, raw_body)
    held_sms_store.set_alert_id([sid], alert_id)
    return "held"
