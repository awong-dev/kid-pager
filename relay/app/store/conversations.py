"""Group `conversations/{convKey}` CRUD: `kind: 'group'`, a minted `g_` id, `uids` as the members.
The group alias shares the flat `aliases/{alias}` namespace with user aliases."""

from __future__ import annotations

from google.cloud.firestore import FieldFilter, Transaction

from app.db.firestore import get_db, run_transaction
from app.ids import new_id
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
            },
        )

    run_transaction(_txn)

    fetched = get_conversation(key)
    assert fetched is not None
    return fetched


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

    run_transaction(_txn)
    fetched = get_conversation(conv_key)
    assert fetched is not None
    return fetched
