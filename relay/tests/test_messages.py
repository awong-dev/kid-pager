"""Direct tests of `app.store.messages` against the Firestore emulator, including the
`create_message` wireId dedup invariant."""

from __future__ import annotations

import pytest

from app.db.firestore import get_db
from app.store import messages as messages_store
from app.store import users as users_store


def _count_messages() -> int:
    return len(list(get_db().collection("messages").stream()))


def _make_user(uid: str, alias: str, *, family_id: str | None = None) -> None:
    users_store.create_user(uid=uid, alias=alias, display_name=alias, family_id=family_id)


def test_create_message_same_wire_id_dedups_and_stamps_the_wire_id_doc():
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
    snap = get_db().collection("wireIds").document("w_dupe_bob").get()
    assert snap.exists
    assert snap.get("createdAt") is not None


@pytest.mark.parametrize(
    ("recipient_uid", "recipient_fields", "expected_family_ids"),
    [
        pytest.param("fam_b_1", {"family_id": "fam_b"}, ["fam_a", "fam_b"], id="two-families"),
        pytest.param("ext_1", {"kind": "external"}, ["fam_a"], id="external-recipient"),
    ],
)
def test_create_message_sets_family_ids_from_the_member_families(
    recipient_uid, recipient_fields, expected_family_ids
):
    _make_user("fam_a_1", "fam_a_1", family_id="fam_a")
    users_store.create_user(
        uid=recipient_uid, alias=recipient_uid, display_name=recipient_uid, **recipient_fields
    )

    msg = messages_store.create_message(
        sender_uid="fam_a_1",
        recipient_uid=recipient_uid,
        kind="text",
        ts=1000,
        body="hi",
    )
    assert msg is not None
    assert msg.familyIds == expected_family_ids

    conv = messages_store.get_conversation(msg.convKey)
    assert conv is not None
    assert conv.familyIds == expected_family_ids
    assert set(conv.participants) == {"fam_a_1", recipient_uid}
    assert conv.participants["fam_a_1"].alias == "fam_a_1"
    assert conv.participants[recipient_uid].displayName == recipient_uid


def test_create_message_does_not_rewrite_participants_on_an_existing_conversation():
    _make_user("fam_d_1", "fam_d_1", family_id="fam_d")
    _make_user("fam_d_2", "fam_d_2", family_id="fam_d")

    first = messages_store.create_message(
        sender_uid="fam_d_1", recipient_uid="fam_d_2", kind="text", ts=1000, body="hi"
    )
    assert first is not None
    users_store.update_user("fam_d_2", display_name="renamed")

    second = messages_store.create_message(
        sender_uid="fam_d_1", recipient_uid="fam_d_2", kind="text", ts=1001, body="hi again"
    )
    assert second is not None

    conv = messages_store.get_conversation(first.convKey)
    assert conv is not None
    assert conv.participants["fam_d_2"].displayName == "fam_d_2"
