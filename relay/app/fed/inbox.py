"""What `POST /fed/v1/inbox` does with each event: `msg`, `contact` and `ping` are answered
individually (`ok`, `dup` or `rej`); only a frozen family fails the whole request."""

from __future__ import annotations

import logging
import re
import time
from typing import Any

from app import alerts as alerts_module
from app import sms_text
from app.routing import RejectedRecipient, Routing
from app.store import allow as allow_store
from app.store import externals as externals_store
from app.store import fed_links, fed_state
from app.store import messages as messages_store
from app.store import users as users_store
from app.store.users import ALIAS_RE, User
from app.wire import BODY_MAX_UTF8_BYTES

logger = logging.getLogger("relay.fed.inbox")

MAX_EVENTS = 50
ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
RID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
NAME_MAX = 120
CLOCK_SLACK_S = 300
MAX_AGE_S = 7 * 24 * 3600

# `Routing.send`'s rejection -> the wire `rsn`.
_RSN: dict[str, str] = {
    "policy_in": "policy",
    "policy_out": "policy",
    "hook_blocked": "policy",
    "unknown_alias": "unknown_recipient",
}


def wire_id(remote_fed_id: str, event_id: str) -> str:
    """Unique per origin family and stable across a move of either family."""
    return f"fed_{remote_fed_id}_{event_id}"


def _rej(event_id: str, reason: str) -> dict[str, str]:
    return {"id": event_id, "st": "rej", "rsn": reason}


def _ok(event_id: str) -> dict[str, str]:
    return {"id": event_id, "st": "ok"}


def reason_for(rejected: list[RejectedRecipient]) -> str:
    return _RSN.get(rejected[0].reason, "not_allowed")


def _clean_ts(raw: Any) -> int:
    now = int(time.time())
    if (
        isinstance(raw, int)
        and not isinstance(raw, bool)
        and now - MAX_AGE_S <= raw <= now + CLOCK_SLACK_S
    ):
        return raw
    return now


def _local_recipient(link: fed_links.FedLink, to: Any) -> User | None:
    """`to` as an enabled person of the link's family who is shared on `link`."""
    if not isinstance(to, str) or not RID_RE.match(to) or to not in link.shared:
        return None
    user = users_store.get_user(to)
    if (
        user is None
        or user.kind != "person"
        or user.disabled
        or user.familyId != link.familyId  # a member who moved to another family is not shared
    ):
        return None
    if not fed_state.family_enabled(user.familyId):
        return None  # as if not shared: do not leak that the family exists
    return user


def process(
    link: fed_links.FedLink, events: list[Any], routing: Routing
) -> list[dict[str, str]]:
    """One result per event. `FamilyFrozen` propagates (503 for the request)."""
    results: list[dict[str, str]] = []
    for event in events:
        if not isinstance(event, dict):
            results.append(_rej("", "invalid"))
            continue
        event_id = event.get("id")
        if not isinstance(event_id, str) or not ID_RE.match(event_id):
            results.append(_rej("", "invalid"))
            continue
        kind = event.get("type")
        if kind == "msg":
            results.append(_msg(link, event_id, event, routing))
        elif kind == "contact":
            results.append(_contact(link, event_id, event))
        elif kind == "ping":
            results.append(_ok(event_id))
        else:
            results.append(_rej(event_id, "unsupported"))
    return results


def _msg(
    link: fed_links.FedLink, event_id: str, event: dict, routing: Routing
) -> dict[str, str]:
    sender_rid, body = event.get("from"), event.get("body")
    if (
        not isinstance(sender_rid, str)
        or not RID_RE.match(sender_rid)
        or not isinstance(body, str)
    ):
        return _rej(event_id, "invalid")
    target = _local_recipient(link, event.get("to"))
    if target is None:
        return _rej(event_id, "unknown_recipient")
    assert target.familyId is not None

    wid = wire_id(link.remote.fedId, event_id)
    if messages_store.wire_id_exists(wid, target.uid):
        return {"id": event_id, "st": "dup"}

    external = externals_store.get_family_fed(target.familyId, link.remote.fedId, sender_rid)
    if external is None:
        outcome = alerts_module.fed_unknown(link, target, sender_rid)
        logger.info("fed msg from unknown person link=%s alert=%s", link.id, outcome)
        return _rej(event_id, "not_allowed")
    edge = allow_store.get_edge(target.uid, external.uid)
    if external.disabled or edge is None or not edge.message:
        return _rej(event_id, "not_allowed")

    if len(body.encode("utf-8")) > BODY_MAX_UTF8_BYTES:
        return _rej(event_id, "too_long")
    text = sms_text.pager_body(body)
    if not text:
        return _rej(event_id, "invalid")

    result = routing.send(
        sender_uid=external.uid,
        recipient_alias=target.alias,
        kind="text",
        body=text,
        origin_backend_kind="fed",
        origin_backend_id="fed",
        wire_id=wid,
        ts=_clean_ts(event.get("ts")),
    )
    if result.rejected:
        return _rej(event_id, reason_for(result.rejected))
    if not result.messages:
        return {"id": event_id, "st": "dup"}
    return _ok(event_id)


def _contact(link: fed_links.FedLink, event_id: str, event: dict) -> dict[str, str]:
    who = event.get("from")
    if not isinstance(who, dict):
        return _rej(event_id, "invalid")
    rid, alias, name = who.get("rid"), who.get("alias"), who.get("name")
    if (
        not isinstance(rid, str)
        or not RID_RE.match(rid)
        or not isinstance(alias, str)
        or not ALIAS_RE.match(alias)
        or not isinstance(name, str)
    ):
        return _rej(event_id, "invalid")
    name = sms_text.pager_body(name)[:NAME_MAX]
    if not name:
        return _rej(event_id, "invalid")
    target = _local_recipient(link, event.get("to"))
    if target is None:
        return _rej(event_id, "unknown_recipient")
    assert target.familyId is not None

    external = externals_store.get_family_fed(target.familyId, link.remote.fedId, rid)
    edge = (
        allow_store.get_edge(target.uid, external.uid) if external is not None else None
    )
    if external is not None and edge is not None and edge.message:
        return _ok(event_id)  # added both ways already
    outcome = alerts_module.fed_contact(link, target, rid, alias, name)
    logger.info("fed contact from link=%s alert=%s", link.id, outcome)
    return _ok(event_id)
