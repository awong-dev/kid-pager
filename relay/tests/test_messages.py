"""Direct tests of `app.store.messages` against the Firestore emulator, including the

from __future__ import annotations

from app.db.firestore import get_db
from app.store import messages as messages_store


def _count_messages() -> int:
    return len(list(get_db().collection("messages").stream()))


def test_create_message_same_wire_id_dedups():
    first = messages_store.create_message(
        sender_uid="alice",
        recipient_uid="bob",
        kind="text",
        ts=1000,
        body="hi",
        wire_id="w_dupe",
    )
    assert first is not None

    second = messages_store.create_message(
        sender_uid="alice",
        recipient_uid="bob",
        kind="text",
        ts=1001,
        body="hi again",
        wire_id="w_dupe",
    )
    assert second is None

    assert _count_messages() == 1


def test_create_message_wire_id_doc_carries_created_at():
    msg = messages_store.create_message(
        sender_uid="alice",
        recipient_uid="carol",
        kind="text",
        ts=1000,
        body="hi",
        wire_id="w_stamped",
    )
    assert msg is not None

    snap = get_db().collection("wireIds").document("w_stamped_carol").get()
    assert snap.exists
    assert snap.get("createdAt") is not None
