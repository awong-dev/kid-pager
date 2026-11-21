"""Adding a remote person: the local external, allow edges, a book push and one queued `contact`
event per member; members must be in the link's `shared` list."""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Iterable

from app import book
from app.broker import BrokerClient
from app.fed import sender
from app.store import allow as allow_store
from app.store import externals as externals_store
from app.store import fed_links, fed_outbox
from app.store import users as users_store
from app.store.users import User

logger = logging.getLogger("relay.fed.contacts")


class NotShared(Exception):
    """A chosen member is not in the link's `shared` list."""

    def __init__(self, uid: str) -> None:
        super().__init__(f"{uid} is not shared with that family")
        self.uid = uid


class NotAMember(Exception):
    def __init__(self, uid: str) -> None:
        super().__init__(f"{uid} is not a member of this family")
        self.uid = uid


def contact_event_id(link_id: str, member_uid: str, rid: str) -> str:
    return (
        "ct_"
        + hashlib.sha256(f"{link_id}|{member_uid}|{rid}".encode()).hexdigest()[:24]
    )


def check_members(
    family_id: str, link: fed_links.FedLink, member_uids: Iterable[str]
) -> list[User]:
    """The chosen members as users: each an enabled person of `family_id`
    (`NotAMember`) and shared on `link` (`NotShared`)."""
    members: list[User] = []
    for uid in dict.fromkeys(member_uids):
        user = users_store.get_user(uid)
        if (
            user is None
            or user.kind != "person"
            or user.familyId != family_id
            or user.disabled
        ):
            raise NotAMember(uid)
        if uid not in link.shared:
            raise NotShared(uid)
        members.append(user)
    return members


def queue_contact_events(
    link: fed_links.FedLink, rid: str, members: Iterable[User]
) -> None:
    """One pending `contact` event per member: `{type, id, from:{rid, alias, name},
    to: rid}`. The row id is derived from the event id, so repeating an add queues
    nothing new."""
    for member in members:
        event_id = contact_event_id(link.id, member.uid, rid)
        fed_outbox.enqueue(
            "ob_" + event_id,
            link.id,
            link.familyId,
            {
                "type": "contact",
                "id": event_id,
                "from": {
                    "rid": member.uid,
                    "alias": member.alias,
                    "name": member.displayName,
                },
                "to": rid,
            },
        )


def add_remote_person(
    *,
    family_id: str,
    link: fed_links.FedLink,
    rid: str,
    remote_alias: str,
    remote_name: str,
    members: list[User],
    broker: BrokerClient,
    alias: str | None = None,
    name: str | None = None,
) -> User:
    """Creates (or finds) the external, gives each member an outgoing `message`
    edge, pushes their books, and queues the `contact` events. Raises
    `externals_store.ContactNameTaken`, `users_store.AliasTaken`,
    `users_store.InvalidAlias` for the caller to turn into a question for the
    admin; nothing but the external's own rows is written before those."""
    external = externals_store.get_or_create_fed(
        family_id, link.remote.fedId, rid, remote_alias, name or remote_name, alias=alias
    )
    for member in members:
        allow_store.set_edge(member.uid, external.uid, message=True, locate=False)
    book.bump_and_push({m.uid for m in members}, broker, reason="fed_contact")
    queue_contact_events(link, rid, members)
    sender.kick(link.id)
    logger.info(
        "fed contact added family=%s link=%s members=%d",
        family_id,
        link.id,
        len(members),
    )
    return external
