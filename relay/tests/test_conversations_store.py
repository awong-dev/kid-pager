"""`app.store.conversations` and the group-aware parts of `app.store.messages`."""

from __future__ import annotations

import pytest
from google.api_core.exceptions import AlreadyExists

from app.store import conversations as conversations_store
from app.store import messages as messages_store
from app.store import users as users_store


def _make_user(uid: str, alias: str, *, family_id: str | None = None) -> None:
    users_store.create_user(uid=uid, alias=alias, display_name=alias, family_id=family_id)


def test_create_group_returns_conversation_with_sorted_members():
    _make_user("carol", "carol")
    _make_user("alice", "alice")
    _make_user("bob", "bob")

    conv = conversations_store.create_group(
        name="Family",
        alias="family",
        member_uids=["carol", "alice", "bob", "alice"],
        created_by="alice",
    )

    assert conv.kind == "group"
    assert conv.name == "Family"
    assert conv.alias == "family"
    assert conv.createdBy == "alice"
    assert conv.uids == ["alice", "bob", "carol"]
    assert conv.convKey.startswith("g_")


@pytest.mark.parametrize(
    ("alias", "taken_by_group"),
    [("dupe-group", True), ("alice2", False)],
    ids=["group_alias", "user_alias"],
)
def test_create_group_alias_collision_raises_already_exists(alias: str, taken_by_group: bool):
    """The alias namespace is shared: a group cannot take a user's alias, or vice versa."""
    _make_user("alice2", "alice2")
    _make_user("bob2", "bob2")
    if taken_by_group:
        conversations_store.create_group(
            name="Family", alias=alias, member_uids=["alice2", "bob2"], created_by="alice2"
        )

    with pytest.raises(AlreadyExists):
        conversations_store.create_group(
            name="Family Again", alias=alias, member_uids=["alice2", "bob2"], created_by="alice2"
        )


def test_create_group_requires_two_distinct_members():
    _make_user("solo", "solo")
    with pytest.raises(ValueError):
        conversations_store.create_group(
            name="Solo", alias="solo-group", member_uids=["solo", "solo"], created_by="solo"
        )


@pytest.mark.parametrize("alias", ["nope-does-not-exist", "harry"], ids=["unknown_alias", "user_alias"])
def test_get_by_alias_none_for_a_non_group_alias(alias: str):
    """A user's `aliases/{alias}` doc has `uid`, not `convKey`: the group lookup ignores it."""
    _make_user("harry", "harry")
    assert conversations_store.get_by_alias(alias) is None


def test_add_member_then_remove_member():
    _make_user("iris", "iris")
    _make_user("jack", "jack")
    _make_user("kate", "kate")
    conv = conversations_store.create_group(
        name="Trio", alias="trio", member_uids=["iris", "jack"], created_by="iris"
    )

    assert conversations_store.is_member(conv.convKey, "kate") is False
    added = conversations_store.add_member(conv.convKey, "kate")
    assert added.uids == ["iris", "jack", "kate"]
    assert conversations_store.is_member(conv.convKey, "kate") is True

    # Adding an existing member is a no-op, not an error or a duplicate.
    again = conversations_store.add_member(conv.convKey, "kate")
    assert again.uids == ["iris", "jack", "kate"]

    removed = conversations_store.remove_member(conv.convKey, "jack")
    assert removed.uids == ["iris", "kate"]
    assert conversations_store.is_member(conv.convKey, "jack") is False


def test_add_member_unknown_conversation_raises_key_error():
    with pytest.raises(KeyError):
        conversations_store.add_member("g_doesnotexist", "someone")


def test_group_copies_share_one_seq_and_group_msg_id():
    _make_user("owner1", "owner1")
    _make_user("member1", "member1")
    _make_user("member2", "member2")
    conv = conversations_store.create_group(
        name="G",
        alias="g-seq-test",
        member_uids=["owner1", "member1", "member2"],
        created_by="owner1",
    )

    seq = messages_store.allocate_seq()
    group_msg_id = "gm_test1"

    copy_a = messages_store.create_message(
        sender_uid="owner1",
        recipient_uid="member1",
        kind="text",
        ts=1000,
        body="hello group",
        conv_key=conv.convKey,
        uids=sorted(["owner1", "member1"]),
        seq=seq,
        group_msg_id=group_msg_id,
        sender_alias="owner1",
    )
    copy_b = messages_store.create_message(
        sender_uid="owner1",
        recipient_uid="member2",
        kind="text",
        ts=1000,
        body="hello group",
        conv_key=conv.convKey,
        uids=sorted(["owner1", "member2"]),
        seq=seq,
        group_msg_id=group_msg_id,
        sender_alias="owner1",
    )

    assert copy_a is not None
    assert copy_b is not None
    assert copy_a.seq == copy_b.seq == seq
    assert copy_a.groupMsgId == copy_b.groupMsgId == group_msg_id
    assert copy_a.convKey == copy_b.convKey == conv.convKey
    # Each copy's `uids` stays the sender/recipient pair; the member list is on the conversation.
    assert copy_a.uids == sorted(["owner1", "member1"])
    assert copy_b.uids == sorted(["owner1", "member2"])

    # The conversation's member list survives two `create_message` calls (uids are not overwritten).
    conv_after = messages_store.get_conversation(conv.convKey)
    assert conv_after is not None
    assert conv_after.uids == ["member1", "member2", "owner1"]
    assert conv_after.unread["member1"] == 1
    assert conv_after.unread["member2"] == 1
    assert "owner1" not in conv_after.unread


def test_group_family_ids_and_participants_follow_create_join_and_leave():
    for suffix in "abcd":
        _make_user(f"g_fam_{suffix}", f"g_fam_{suffix}", family_id=f"fam_{suffix}")

    conv = conversations_store.create_group(
        name="Three families",
        alias="three-families",
        member_uids=["g_fam_a", "g_fam_b", "g_fam_c"],
        created_by="g_fam_a",
    )
    assert conv.familyIds == ["fam_a", "fam_b", "fam_c"]
    assert set(conv.participants) == {"g_fam_a", "g_fam_b", "g_fam_c"}
    assert conv.participants["g_fam_b"].alias == "g_fam_b"

    joined = conversations_store.add_member(conv.convKey, "g_fam_d")
    assert joined.familyIds == ["fam_a", "fam_b", "fam_c", "fam_d"]
    assert "g_fam_d" in joined.participants
    assert joined.participants["g_fam_d"].alias == "g_fam_d"

    left = conversations_store.remove_member(conv.convKey, "g_fam_b")
    assert left.familyIds == ["fam_a", "fam_c", "fam_d"]
    assert "g_fam_b" not in left.participants
