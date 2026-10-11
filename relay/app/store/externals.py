"""External contacts: `users/{uid}` with `kind: 'external'`, owned by one family (`ownerFamilyId`).
Ids derive from `h = sha256(f"{fid}|{e164}")`; names are unique per family via `contactNames`."""

from __future__ import annotations

import hashlib

import phonenumbers
from google.api_core.exceptions import AlreadyExists

from app.db.firestore import get_db
from app.store import allow as allow_store
from app.store import backends as backends_store
from app.store import users as users_store
from app.store.users import User

# Default region "US": a bare 10-digit number resolves the way a US phone is typed.
DEFAULT_REGION = "US"


def normalize_phone(phone: str, *, default_region: str = DEFAULT_REGION) -> str:
    """E.164 for `phone` via `is_possible_number` (555 numbers pass); `ValueError` if not."""
    try:
        parsed = phonenumbers.parse(phone, default_region)
    except phonenumbers.NumberParseException as exc:
        raise ValueError(f"not a valid phone number: {phone!r}") from exc
    if not phonenumbers.is_possible_number(parsed):
        raise ValueError(f"not a valid phone number: {phone!r}")
    return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)


def contact_ids(family_id: str, e164: str) -> tuple[str, str]:
    return f"x_{h[:16]}", f"x{h[:11]}"


def get_family_contact(family_id: str, phone: str) -> User | None:
    """The family's SMS contact for `phone` (any spelling), or `None`."""
    uid, _alias = contact_ids(family_id, normalize_phone(phone))
    user = users_store.get_user(uid)
    if user is None or user.kind != "external":
        return None
    return user


def list_family_contacts(family_id: str) -> list[User]:
    return [
        u for u in users_store.list_users() if u.kind == "external" and u.ownerFamilyId == family_id
    ]


class ContactNameTaken(ValueError):
    """Another contact of the same family already uses this name."""


def name_key(name: str) -> str:
    from app.book import truncate_sms_name

    return truncate_sms_name(name).casefold()


def _name_ref(family_id: str, key: str):
    digest = hashlib.sha256(key.encode()).hexdigest()[:16]
    return get_db().collection("contactNames").document(f"{family_id}_{digest}")


def _reserve(family_id: str, key: str, uid: str, name: str) -> None:
    ref = _name_ref(family_id, key)
    try:
        ref.create({"uid": uid, "familyId": family_id})
    except AlreadyExists:
        holder = (ref.get().to_dict() or {}).get("uid")
        if holder == uid:
            return
        raise ContactNameTaken(f'a contact named "{name}" already exists') from None


def _release(family_id: str, key: str, uid: str) -> None:
    ref = _name_ref(family_id, key)
    snap = ref.get()
    if snap.exists and (snap.to_dict() or {}).get("uid") == uid:
        ref.delete()


def get_or_create(family_id: str, phone: str, display_name: str) -> User:
    """Idempotent on `(family_id, phone)`, normalized; `ContactNameTaken` on a name clash."""
    e164 = normalize_phone(phone)
    uid, alias = contact_ids(family_id, e164)
    existing = users_store.get_user(uid)
    if existing is not None:
        ensure_sms_backend(existing)
        return existing

    key = name_key(display_name)
    _reserve(family_id, key, uid, display_name)
    try:
        users_store.create_user(
            uid=uid,
            alias=alias,
            display_name=display_name,
            phone=e164,
            family_id=None,
            kind="external",
            owner_family_id=family_id,
        )
    except users_store.AliasTaken as exc:
        if users_store.get_uid_for_alias(alias) != uid:
            _release(family_id, key, uid)
            raise ValueError("contact alias collision") from exc
        # A concurrent create of the same contact won the race.
        raced = users_store.get_user(uid)
        if raced is None:
            _release(family_id, key, uid)
            raise ValueError("contact alias collision") from exc
        ensure_sms_backend(raced)
        return raced
    except Exception:
        _release(family_id, key, uid)
        raise

    fetched = users_store.get_user(uid)
    assert fetched is not None
    ensure_sms_backend(fetched)
    return fetched


def ensure_sms_backend(user: User) -> str:
    """The id of an external's `sms` backend row, created if missing. Idempotent."""
    for b in backends_store.list_backends(user.uid):
        if b.kind == "sms":
            return b.id
    # Fixed id: two racing callers write the same doc, never two rows (a
    # duplicate row would text the contact twice).
    created = backends_store.create_backend(
        user.uid, kind="sms", config={"phone": user.phone or ""}, enabled=True, bid="sms"
    )
    return created.id


def ensure_bridge_backend(user: User, config: dict) -> str:
    """The id of an external's `bridge` backend row (fixed id `bridge`), created or refreshed."""
    for b in backends_store.list_backends(user.uid):
        if b.kind == "bridge":
            if b.config != config:
                backends_store.update_backend(user.uid, b.id, config=config)
            return b.id
    created = backends_store.create_backend(
        user.uid, kind="bridge", config=config, enabled=True, bid="bridge"
    )
    return created.id


def chat_ids(bridge_id: str, conversation_id: str) -> tuple[str, str, str]:
    """`(uid, alias, h)` of a bridge conversation's external: `x_c`+h[:16], alias `c`+h[:11]."""
    h = hashlib.sha256(f"{bridge_id}|{conversation_id}".encode()).hexdigest()
    return f"x_c{h[:16]}", f"c{h[:11]}", h


def get_or_create_chat(
    family_id: str,
    bridge_id: str,
    conversation_id: str,
    display_name: str,
    *,
    source: str,
    link: str | None,
    is_group: bool,
    title: str | None,
    can_reply: bool,
) -> User:
    """Idempotent on `(bridge_id, conversation_id)`; an existing user keeps its name and `chat`."""
    uid, alias, _h = chat_ids(bridge_id, conversation_id)
    config = {
        "bridgeId": bridge_id,
        "source": source,
        "conversationId": conversation_id,
        "link": link,
    }
    existing = users_store.get_user(uid)
    if existing is not None:
        ensure_bridge_backend(existing, config)
        return existing

    key = name_key(display_name)
    _reserve(family_id, key, uid, display_name)
    chat = {
        "bridgeId": bridge_id,
        "conversationId": conversation_id,
        "source": source,
        "link": link,
        "isGroup": is_group,
        "title": title,
        "canReply": can_reply,
    }
    try:
        users_store.create_user(
            uid=uid,
            alias=alias,
            display_name=display_name,
            phone=None,
            family_id=None,
            kind="external",
            owner_family_id=family_id,
            chat=chat,
        )
    except users_store.AliasTaken as exc:
        raced = users_store.get_user(uid)
        if raced is None:
            _release(family_id, key, uid)
            raise ValueError("contact alias collision") from exc
        ensure_bridge_backend(raced, config)
        return raced
    except Exception:
        _release(family_id, key, uid)
        raise
    fetched = users_store.get_user(uid)
    assert fetched is not None
    ensure_bridge_backend(fetched, config)
    return fetched


def clear_member_channels(family_id: str, member_uid: str) -> int:
    """Drops `member_uid` from every contact's `config.via`/`config.voiceConv`; returns a count."""
    changed = 0
    for ext in list_family_contacts(family_id):
        row = backends_store.get_backend(ext.uid, "sms")
        if row is None:
            continue
        config = dict(row.config)
        touched = False
        for key in ("via", "voiceConv"):
            mapping = config.get(key)
            if isinstance(mapping, dict) and member_uid in mapping:
                mapping = {k: v for k, v in mapping.items() if k != member_uid}
                if mapping:
                    config[key] = mapping
                else:
                    config.pop(key)
                touched = True
        if touched:
            backends_store.update_backend(ext.uid, row.id, config=config)
            changed += 1
    return changed


def update_chat(uid: str, **fields: object) -> None:
    """Merges `fields` into `users/{uid}.chat` (dotted-path update)."""
    get_db().collection("users").document(uid).update(
        {f"chat.{key}": value for key, value in fields.items()}
    )


def rename(family_id: str, uid: str, new_name: str) -> User:
    """Renames the family's contact: reserve new, update, release old; `ContactNameTaken`."""
    user = users_store.get_user(uid)
    if user is None:
        raise KeyError(f"no such user: {uid!r}")
    old_key, new_key = name_key(user.displayName), name_key(new_name)
    if old_key == new_key:
        _reserve(family_id, new_key, uid, new_name)
        return users_store.update_user(uid, display_name=new_name)
    _reserve(family_id, new_key, uid, new_name)
    try:
        updated = users_store.update_user(uid, display_name=new_name)
    except Exception:
        _release(family_id, new_key, uid)
        raise
    _release(family_id, old_key, uid)
    return updated


def delete(family_id: str, uid: str) -> None:
    """Removes the contact, its `allow` edges, name reservation, backend and book docs."""
    user = users_store.get_user(uid)
    if user is None:
        return
    for edge in allow_store.list_edges():
        if edge.fromUid == uid or edge.toUid == uid:
            allow_store.delete_edge(edge.fromUid, edge.toUid)
    _release(family_id, name_key(user.displayName), uid)
    user_ref = get_db().collection("users").document(uid)
    for sub in ("backends", "book"):
        for snap in user_ref.collection(sub).stream():
            snap.reference.delete()
    users_store.delete_user(uid)
