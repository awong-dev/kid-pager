"""Group `conversations/{convKey}` CRUD: `kind: 'group'`, a minted `g_` id, `uids` as the members.
The group alias shares the flat `aliases/{alias}` namespace with user aliases."""

from __future__ import annotations

from google.api_core.exceptions import AlreadyExists
from google.cloud.firestore import FieldFilter, Transaction

from app.db.firestore import get_db, run_transaction
from app.ids import new_id
from app.store.messages import (
    Conversation,
    build_participants_and_family_ids,
    get_conversation,
)
from app.store.users import ALIAS_RE, RESERVED_ALIASES, InvalidAlias


def _conversations():
    return get_db().collection("conversations")


def _aliases():
    return get_db().collection("aliases")


def _validate_group_alias(alias: str) -> None:
    # Same regex/reserved-word rule as a user alias: it is the same flat namespace.
    if alias in RESERVED_ALIASES or not ALIAS_RE.match(alias):
        raise InvalidAlias(f"invalid or reserved group alias: {alias!r}")


def create_group(
    *,
    name: str,
    alias: str,
    member_uids: list[str],
    created_by: str,
) -> Conversation:
    """Creates a group and its alias in one transaction; `AlreadyExists` if the alias is taken."""
    _validate_group_alias(alias)
    members = sorted(set(member_uids))
    if len(members) < 2:
        raise ValueError("a group needs at least two distinct members")

    key = new_id("g_")
    conv_ref = _conversations().document(key)
    alias_ref = _aliases().document(alias)

    def _txn(transaction: Transaction) -> None:
        # `participants`/`familyIds` over the full member list, written once on creation.
        participants, family_ids = build_participants_and_family_ids(
            members, transaction=transaction
        )
        transaction.create(alias_ref, {"convKey": key})
        transaction.set(
            conv_ref,
            {
                "uids": members,
                "kind": "group",
                "name": name,
                "alias": alias,
                "createdBy": created_by,
                "lastMessageAt": None,
                "lastPreview": "",
                "unread": {},
                "participants": participants,
                "familyIds": family_ids,
            },
        )

    run_transaction(_txn)

    fetched = get_conversation(key)
    assert fetched is not None
    return fetched


def _bridge_ids(bridge_id: str, conversation_id: str) -> tuple[str, str]:
    """`(convKey, alias)` of a bridged group, derived from the same hash as `externals.chat_ids`."""
    import hashlib

    h = hashlib.sha256(f"{bridge_id}|{conversation_id}".encode()).hexdigest()
    return f"g_c{h[:14]}", f"bc{h[:8]}"


def create_bridge_group(
    *,
    name: str,
    owner_uid: str,
    external_uid: str,
    bridge_id: str,
    conversation_id: str,
    source: str,
    link: str | None,
    roster: dict[str, str],
) -> Conversation:
    """The group doc of a subscribed Google Chat group; `AlreadyExists` returns the existing one."""
    key, alias = _bridge_ids(bridge_id, conversation_id)
    conv_ref = _conversations().document(key)
    alias_ref = _aliases().document(alias)
    members = sorted({owner_uid, external_uid})

    def _txn(transaction: Transaction) -> None:
        participants, family_ids = build_participants_and_family_ids(
            members, transaction=transaction
        )
        transaction.create(alias_ref, {"convKey": key})
        transaction.create(
            conv_ref,
            {
                "uids": members,
                "kind": "group",
                "name": name,
                "alias": alias,
                "createdBy": owner_uid,
                "lastMessageAt": None,
                "lastPreview": "",
                "unread": {},
                "participants": participants,
                "familyIds": family_ids,
                "bridge": {
                    "bridgeId": bridge_id,
                    "conversationId": conversation_id,
                    "source": source,
                    "link": link,
                },
                "roster": roster,
            },
        )

    try:
        run_transaction(_txn)
    except AlreadyExists:
        pass
    fetched = get_conversation(key)
    assert fetched is not None
    return fetched


def get_bridge_group(conv_key: str) -> Conversation | None:
    conv = get_conversation(conv_key)
    return conv if conv is not None and conv.bridge is not None else None


def update_bridge_group(
    conv_key: str, *, name: str | None = None, roster: dict[str, str] | None = None
) -> None:
    updates: dict[str, object] = {}
    if name is not None:
        updates["name"] = name
    if roster is not None:
        updates["roster"] = roster
    if updates:
        _conversations().document(conv_key).update(updates)


def delete_bridge_group(conv_key: str) -> None:
    """The group doc and its alias; messages keep their history."""
    conv = get_conversation(conv_key)
    if conv is None:
        return
    if conv.alias:
        _aliases().document(conv.alias).delete()
    _conversations().document(conv_key).delete()


def get_by_alias(alias: str) -> Conversation | None:
    """Resolves a group alias to its conversation; None if absent or it names a user."""
    snap = _aliases().document(alias).get()
    if not snap.exists:
        return None
    key = (snap.to_dict() or {}).get("convKey")
    if key is None:
        return None
    return get_conversation(key)


def list_groups_for_member(uid: str) -> list[Conversation]:
    """Every group conversation `uid` belongs to (`array-contains`, filtered in Python)."""
    query = _conversations().where(filter=FieldFilter("uids", "array_contains", uid))
    return [
        conv
        for conv in (
            Conversation.model_validate({"convKey": snap.id, **(snap.to_dict() or {})})
            for snap in query.stream()
        )
        if conv.kind == "group"
    ]


def is_member(conv_key: str, uid: str) -> bool:
    conv = get_conversation(conv_key)
    return conv is not None and uid in conv.uids


def add_member(conv_key: str, uid: str) -> Conversation:
    """Adds `uid` to `uids` (no-op if a member); creating `allow` edges is the caller's job."""
    ref = _conversations().document(conv_key)

    def _txn(transaction: Transaction) -> None:
        snap = ref.get(transaction=transaction)
        if not snap.exists:
            raise KeyError(f"no such conversation: {conv_key!r}")
        members = sorted(set(snap.get("uids") or []) | {uid})
        # A join rewrites `participants` and `familyIds`.
        participants, family_ids = build_participants_and_family_ids(
            members, transaction=transaction
        )
        transaction.update(
            ref, {"uids": members, "participants": participants, "familyIds": family_ids}
        )

    run_transaction(_txn)
    fetched = get_conversation(conv_key)
    assert fetched is not None
    return fetched


def remove_member(conv_key: str, uid: str) -> Conversation:
    """Removes `uid` from `uids` (no-op if not a member); leaves `allow` edges untouched."""
    ref = _conversations().document(conv_key)

    def _txn(transaction: Transaction) -> None:
        snap = ref.get(transaction=transaction)
        if not snap.exists:
            raise KeyError(f"no such conversation: {conv_key!r}")
        members = sorted(set(snap.get("uids") or []) - {uid})
        # A leave rewrites `participants` and `familyIds`.
        participants, family_ids = build_participants_and_family_ids(
            members, transaction=transaction
        )
        transaction.update(
            ref, {"uids": members, "participants": participants, "familyIds": family_ids}
        )

    run_transaction(_txn)
    fetched = get_conversation(conv_key)
    assert fetched is not None
    return fetched
