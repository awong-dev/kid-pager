"""Unit tests for `app.routing.Routing`: allow-list enforcement, recipient resolution,
origin-backend exclusion, the drop+system-reply path and inline delivery via the fake broker."""

from __future__ import annotations

import json

import pytest

from app.backends.registry import build_registry
from app.db.firestore import get_db
from app.routing import RejectedRecipient, Routing
from app.store import alerts as alerts_store
from app.store import allow as allow_store
from app.store import backends as backends_store
from app.store import conversations as conversations_store
from app.store import devices as devices_store
from app.store import externals as externals_store
from app.store import families as families_store
from app.store import messages as messages_store
from app.store import push_tokens as push_tokens_store
from app.store import users as users_store
from tests.fake_transport import FakeBrokerClient


@pytest.fixture
def broker() -> FakeBrokerClient:
    return FakeBrokerClient()


@pytest.fixture
def routing(broker: FakeBrokerClient) -> Routing:
    return Routing(broker)


def _make_user(uid: str, alias: str, *, family_id: str | None = None) -> None:
    users_store.create_user(uid=uid, alias=alias, display_name=alias, family_id=family_id)


def _make_pager_device(device_id: str, owner_uid: str, *, default_to_uid: str | None = None):
    devices_store.create_device(
        device_id=device_id,
        owner_uid=owner_uid,
        label="d",
        mqtt_username=device_id,
        mqtt_password_hash="x",
        default_to_uid=default_to_uid,
        auth_mode="password",
    )
    return backends_store.create_backend(
        owner_uid, kind="pager", config={"deviceId": device_id}, enabled=True
    )


def test_send_to_disallowed_recipient_is_dropped_and_rejected(routing: Routing):
    _make_user("alice", "alice")
    _make_user("bob", "bob")
    # No allow edge between alice and bob.

    result = routing.send(
        sender_uid="alice", recipient_alias="bob", kind="text", body="hi", origin_backend_kind="webapp"
    )
    assert result.messages == []
    assert len(result.rejected) == 1
    assert result.rejected[0].reason == "not_allowed"
    assert result.rejected[0].uid == "bob"


def test_send_to_unknown_alias_is_rejected(routing: Routing):
    _make_user("alice", "alice")
    result = routing.send(
        sender_uid="alice",
        recipient_alias="nobody",
        kind="text",
        body="hi",
        origin_backend_kind="webapp",
    )
    assert result.messages == []
    assert result.rejected == [RejectedRecipient(alias="nobody", uid=None, reason="unknown_alias")]


def test_send_to_allowed_recipient_creates_message(routing: Routing):
    _make_user("alice", "alice")
    _make_user("bob", "bob")
    allow_store.set_edge("alice", "bob", message=True, locate=True)
    allow_store.set_edge("bob", "alice", message=True, locate=True)

    result = routing.send(
        sender_uid="alice", recipient_alias="bob", kind="text", body="hi", origin_backend_kind="webapp"
    )
    assert result.rejected == []
    assert len(result.messages) == 1
    assert result.messages[0].senderUid == "alice"
    assert result.messages[0].recipientUid == "bob"
    assert result.messages[0].body == "hi"


@pytest.mark.parametrize(
    ("pager_devices", "scope_to_backend_id", "expected_kinds", "expected_publishes"),
    [
        pytest.param(1, False, {"webapp"}, 0, id="origin-kind-excludes-same-kind"),
        pytest.param(2, True, {"pager", "webapp"}, 1, id="origin-backend-id-excludes-exact-backend"),
    ],
)
def test_self_send_excludes_the_origin_backend_only(
    routing: Routing, broker, pager_devices, scope_to_backend_id, expected_kinds, expected_publishes
):
    _make_user("loopy", "loopy")
    allow_store.set_edge("loopy", "loopy", message=True, locate=True)
    backends = [_make_pager_device(f"pgr-loopy-{i}", "loopy") for i in range(pager_devices)]

    result = routing.send(
        sender_uid="loopy",
        recipient_alias="loopy",
        kind="text",
        body="hi",
        origin_backend_kind="pager",
        origin_backend_id=backends[0].id if scope_to_backend_id else None,
    )
    assert len(result.messages) == 1
    msg = result.messages[0]
    assert {d.kind for d in msg.deliveries.values()} == expected_kinds
    assert len(broker.published) == expected_publishes
    if scope_to_backend_id:
        assert msg.originBackendKind == "pager"
        assert msg.originBackendId == backends[0].id
        assert backends[0].id not in msg.deliveries
        assert backends[1].id in msg.deliveries


@pytest.mark.parametrize("origin_kind", ["pager", "webapp"])
def test_dm_to_another_user_still_delivers_to_their_pager(routing: Routing, broker, origin_kind):
    _make_user("alice", "alice")
    _make_user("bob", "bob")
    allow_store.set_edge("alice", "bob", message=True, locate=True)
    allow_store.set_edge("bob", "alice", message=True, locate=True)
    _make_pager_device("pgr-bob", "bob")

    result = routing.send(
        sender_uid="alice",
        recipient_alias="bob",
        kind="text",
        body="hi",
        origin_backend_kind=origin_kind,
    )
    msg = result.messages[0]
    assert {d.kind for d in msg.deliveries.values()} == {"webapp", "pager"}
    assert len(broker.published) == 1
    assert broker.published[0].topic == "pager/pgr-bob/down"
    payload = json.loads(broker.published[0].payload)
    assert payload["id"] == msg.id
    assert payload["from"] == "alice"
    assert payload["body"] == "hi"

    # And the pager delivery was marked 'sent' by PagerBackend.deliver().
    refreshed = messages_store.get_message(msg.id)
    pager_bid = next(bid for bid, d in refreshed.deliveries.items() if d.kind == "pager")
    assert refreshed.deliveries[pager_bid].state == "sent"


def test_dm_pager_page_bytes_are_byte_identical_to_before_sndr(routing: Routing, broker):
    _make_user("dmalice", "dmalice")
    _make_user("dmbob", "dmbob")
    allow_store.set_edge("dmalice", "dmbob", message=True, locate=True)
    allow_store.set_edge("dmbob", "dmalice", message=True, locate=True)
    _make_pager_device("pgr-dmbob", "dmbob")

    result = routing.send(
        sender_uid="dmalice",
        recipient_alias="dmbob",
        kind="text",
        body="hi",
        origin_backend_kind="webapp",
    )
    msg = result.messages[0]
    assert len(broker.published) == 1
    expected = (
        f'{{"v":1,"id":"{msg.id}","ts":{msg.ts},"from":"dmalice","body":"hi","ack":null}}'
    ).encode()
    assert broker.published[0].payload == expected


@pytest.mark.parametrize(
    ("sender", "others", "default_to_uid", "expected"),
    [
        pytest.param("bob", ["alice"], "alice", {"alice"}, id="device-default"),
        pytest.param("student", ["mom", "dad"], None, {"mom", "dad"}, id="no-default-broadcasts"),
    ],
)
def test_send_without_alias_goes_to_the_device_default_or_every_allowed_recipient(
    routing: Routing, broker, sender, others, default_to_uid, expected
):
    _make_user(sender, sender)
    for other in others:
        _make_user(other, other)
        allow_store.set_edge(sender, other, message=True, locate=True)
        allow_store.set_edge(other, sender, message=True, locate=True)
    _make_pager_device(f"pgr-{sender}", sender, default_to_uid=default_to_uid)

    result = routing.send(
        sender_uid=sender,
        recipient_alias=None,
        kind="text",
        body="hi",
        origin_backend_kind="pager",
        device_id=f"pgr-{sender}",
    )
    assert len(result.messages) == len(expected)
    assert {m.recipientUid for m in result.messages} == expected


@pytest.mark.parametrize(
    ("users", "group_alias", "recipient_alias", "first_count"),
    [
        pytest.param(["student", "mom"], None, "mom", 1, id="dm"),
        pytest.param(["gowner5", "gmem5", "gmem6"], "fam-dedupe", "fam-dedupe", 2, id="group"),
    ],
)
def test_wire_id_redelivery_creates_no_duplicates(
    routing: Routing, users, group_alias, recipient_alias, first_count
):
    for uid in users:
        _make_user(uid, uid)
    _all_edges(users)
    if group_alias:
        conversations_store.create_group(
            name="Family", alias=group_alias, member_uids=users, created_by=users[0]
        )

    def send():
        return routing.send(
            sender_uid=users[0],
            recipient_alias=recipient_alias,
            kind="text",
            body="hi",
            origin_backend_kind="pager",
            wire_id="u_dupe1234",
        )

    assert len(send().messages) == first_count

    # Same wire_id again (e.g. an at-least-once webhook redelivery).
    second = send()
    assert second.messages == []
    assert second.rejected == []


def _all_edges(uids: list[str]) -> None:
    for a in uids:
        for b in uids:
            if a != b:
                allow_store.set_edge(a, b, message=True, locate=True)


def test_group_send_fans_out_to_every_member_but_sender(routing: Routing):
    _make_user("gowner", "gowner")
    _make_user("gmem1", "gmem1")
    _make_user("gmem2", "gmem2")
    _all_edges(["gowner", "gmem1", "gmem2"])
    group = conversations_store.create_group(
        name="Family",
        alias="fam-routing",
        member_uids=["gowner", "gmem1", "gmem2"],
        created_by="gowner",
    )

    result = routing.send(
        sender_uid="gowner",
        recipient_alias="fam-routing",
        kind="text",
        body="hi all",
        origin_backend_kind="webapp",
    )
    assert result.rejected == []
    assert len(result.messages) == 2
    recipients = {m.recipientUid for m in result.messages}
    assert recipients == {"gmem1", "gmem2"}
    assert "gowner" not in recipients

    # One seq, one groupMsgId, shared convKey -- every member's orderBy(seq)
    # agrees, and the web client can dedupe the sender's own N-1 copies.
    seqs = {m.seq for m in result.messages}
    group_msg_ids = {m.groupMsgId for m in result.messages}
    conv_keys = {m.convKey for m in result.messages}
    assert len(seqs) == 1
    assert len(group_msg_ids) == 1
    assert conv_keys == {group.convKey}
    for m in result.messages:
        assert m.senderAlias == "gowner"
        # The message document's `uids` stays the sender/recipient pair, never the full member list.
        assert set(m.uids) == {"gowner", m.recipientUid}


def test_group_send_message_family_ids_are_per_pair_not_whole_group(routing: Routing):
    _make_user("fowner", "fowner", family_id="fam_x")
    _make_user("fmem1", "fmem1", family_id="fam_y")
    _make_user("fmem2", "fmem2", family_id="fam_z")
    _all_edges(["fowner", "fmem1", "fmem2"])
    group = conversations_store.create_group(
        name="Three fam",
        alias="fam-routing-3",
        member_uids=["fowner", "fmem1", "fmem2"],
        created_by="fowner",
    )
    assert group.familyIds == ["fam_x", "fam_y", "fam_z"]

    result = routing.send(
        sender_uid="fowner",
        recipient_alias="fam-routing-3",
        kind="text",
        body="hi all",
        origin_backend_kind="webapp",
    )
    assert result.rejected == []
    by_recipient = {m.recipientUid: m for m in result.messages}
    assert by_recipient["fmem1"].familyIds == ["fam_x", "fam_y"]
    assert by_recipient["fmem2"].familyIds == ["fam_x", "fam_z"]


def test_group_send_with_two_pagers_publishes_two_pages_no_self_echo(routing: Routing, broker):
    _make_user("gowner3", "gowner3")
    _make_user("gpager_a_owner", "gpager_a_owner")
    _make_user("gpager_b_owner", "gpager_b_owner")
    members = ["gowner3", "gpager_a_owner", "gpager_b_owner"]
    _all_edges(members)
    _make_pager_device("pgr-a", "gpager_a_owner")
    _make_pager_device("pgr-b", "gpager_b_owner")
    conversations_store.create_group(
        name="Family", alias="fam-twopager", member_uids=members, created_by="gowner3"
    )

    result = routing.send(
        sender_uid="gowner3",
        recipient_alias="fam-twopager",
        kind="text",
        body="hi",
        origin_backend_kind="webapp",
    )
    assert len(result.messages) == 2
    assert len(broker.published) == 2
    topics = {p.topic for p in broker.published}
    # Neither pager device is the sender, so both group members with a pager get their own page.
    assert topics == {"pager/pgr-a/down", "pager/pgr-b/down"}


def test_group_pager_page_carries_group_alias_as_from_and_sender_as_sndr(
    routing: Routing, broker
):
    """`from` is the group's alias and `sndr` the author's alias."""
    _make_user("gwireowner", "gwireowner")
    _make_user("gwiremem", "gwiremem")
    _all_edges(["gwireowner", "gwiremem"])
    _make_pager_device("pgr-gwire", "gwiremem")
    conversations_store.create_group(
        name="Family", alias="fam-wire", member_uids=["gwireowner", "gwiremem"], created_by="gwireowner"
    )

    routing.send(
        sender_uid="gwireowner",
        recipient_alias="fam-wire",
        kind="text",
        body="hi",
        origin_backend_kind="webapp",
    )
    assert len(broker.published) == 1
    payload = json.loads(broker.published[0].payload)
    assert payload["from"] == "fam-wire"
    assert payload["sndr"] == "gwireowner"
    assert payload["body"] == "hi"


def test_group_send_by_non_member_is_rejected(routing: Routing):
    _make_user("gowner4", "gowner4")
    _make_user("gmem4", "gmem4")
    _make_user("outsider", "outsider")
    _all_edges(["gowner4", "gmem4", "outsider"])
    conversations_store.create_group(
        name="Family", alias="fam-nonmember", member_uids=["gowner4", "gmem4"], created_by="gowner4"
    )

    result = routing.send(
        sender_uid="outsider",
        recipient_alias="fam-nonmember",
        kind="text",
        body="hi",
        origin_backend_kind="webapp",
    )
    assert result.messages == []
    assert len(result.rejected) == 1
    assert result.rejected[0].reason == "not_member"


def test_group_send_missing_allow_edge_drops_only_that_recipient(routing: Routing, caplog):
    _make_user("gowner6", "gowner6")
    _make_user("gmem7", "gmem7")
    _make_user("gmem8", "gmem8")
    members = ["gowner6", "gmem7", "gmem8"]
    # Only gowner6 <-> gmem7 has an edge; gmem8 is missing one entirely
    # (as if an admin deleted it after the group was created).
    allow_store.set_edge("gowner6", "gmem7", message=True, locate=True)
    allow_store.set_edge("gmem7", "gowner6", message=True, locate=True)
    conversations_store.create_group(
        name="Family", alias="fam-missingedge", member_uids=members, created_by="gowner6"
    )

    with caplog.at_level("WARNING", logger="relay.routing"):
        result = routing.send(
            sender_uid="gowner6",
            recipient_alias="fam-missingedge",
            kind="text",
            body="hi",
            origin_backend_kind="webapp",
        )
    assert {m.recipientUid for m in result.messages} == {"gmem7"}
    assert len(result.rejected) == 1
    assert result.rejected[0].uid == "gmem8"
    assert result.rejected[0].reason == "not_allowed"
    assert any("SECURITY" in rec.message for rec in caplog.records)


def _make_contact(family_id: str, phone: str = "+12065550100", name: str = "Grandma"):
    return externals_store.get_or_create(family_id, phone, name)


def test_dm_to_external_without_sender_number_is_rejected_no_sms_number(
    routing: Routing, caplog
):
    fam = families_store.create_family(name="F", created_by="root").id
    _make_user("alice", "alice", family_id=fam)
    # `open` would otherwise raise a new-conversation alert on a first DM.
    get_db().collection("users").document("alice").update(
        {"policy": {"out": "open", "in": "any"}}
    )
    ext = _make_contact(fam)
    allow_store.set_edge("alice", ext.uid, message=True, locate=False)

    with caplog.at_level("WARNING", logger="relay.routing"):
        result = routing.send(
            sender_uid="alice",
            recipient_alias=ext.alias,
            kind="text",
            body="hi",
            origin_backend_kind="webapp",
        )

    assert result.messages == []
    assert result.rejected == [
        RejectedRecipient(alias=ext.alias, uid=ext.uid, reason="no_sms_number")
    ]
    assert list(get_db().collection("messages").stream()) == []
    assert list(get_db().collection("conversations").stream()) == []
    assert alerts_store.list_alerts(fam, "all") == []
    assert any("cannot reach sms peer" in rec.message for rec in caplog.records)


def test_external_to_external_is_rejected_sms_contact(routing: Routing):
    fam = families_store.create_family(name="F", created_by="root").id
    a = _make_contact(fam, "+12065550100", "A")
    b = _make_contact(fam, "+12065550101", "B")
    result = routing.send(
        sender_uid=a.uid,
        recipient_alias=b.alias,
        kind="text",
        body="hi",
        origin_backend_kind="sms",
    )
    assert result.messages == []
    assert [r.reason for r in result.rejected] == ["sms_contact"]


def test_dm_to_external_without_a_bridge_number_fails_no_bridge(routing: Routing):
    fam = families_store.create_family(name="F", created_by="root").id
    _make_user("alice", "alice", family_id=fam)
    get_db().collection("users").document("alice").update(
        {"policy": {"out": "open", "in": "any"}}
    )
    users_store.set_sms_number("alice", "+12065550777")
    ext = _make_contact(fam)

    result = routing.send(
        sender_uid="alice",
        recipient_alias=ext.alias,
        kind="text",
        body="hi gran",
        origin_backend_kind="webapp",
    )

    assert result.rejected == []
    (msg,) = result.messages
    (d,) = messages_store.get_message(msg.id).deliveries.values()
    assert (d.kind, d.state, d.error) == ("sms", "failed", "no_bridge")


@pytest.mark.parametrize("via_group", [False, True], ids=["broadcast", "group"])
def test_fanout_skips_externals(routing: Routing, via_group):
    fam = families_store.create_family(name="F", created_by="root").id
    _make_user("alice", "alice", family_id=fam)
    _make_user("bob", "bob", family_id=fam)
    ext = _make_contact(fam)
    allow_store.set_edge("alice", "bob", message=True, locate=False)
    allow_store.set_edge("bob", "alice", message=True, locate=False)
    allow_store.set_edge("alice", ext.uid, message=True, locate=False)
    if via_group:
        # A stale membership (create/add refuse an external, but old data may
        # carry one).
        conversations_store.create_group(
            name="Fam", alias="fam-ext", member_uids=["alice", "bob", ext.uid], created_by="alice"
        )

    result = routing.send(
        sender_uid="alice",
        recipient_alias="fam-ext" if via_group else None,
        kind="text",
        body="hi",
        origin_backend_kind="webapp",
    )

    assert {m.recipientUid for m in result.messages} == {"bob"}
    assert result.rejected == []


class _RecordingFCMClient:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], dict[str, str]]] = []

    def send_data(self, tokens: list[str], data: dict[str, str]) -> None:
        self.calls.append((tokens, data))


def test_group_send_push_payload_uses_group_name_alias_and_collapse_key(broker: FakeBrokerClient):
    _make_user("gpushowner", "gpushowner")
    _make_user("gpushmem", "gpushmem")
    _all_edges(["gpushowner", "gpushmem"])
    push_tokens_store.add_token("gpushmem", "tok_gpushmem")
    conv = conversations_store.create_group(
        name="Family", alias="fam-push-route", member_uids=["gpushowner", "gpushmem"], created_by="gpushowner"
    )

    fcm = _RecordingFCMClient()
    routing_with_fcm = Routing(broker, registry=build_registry(broker, fcm_client=fcm))
    routing_with_fcm.send(
        sender_uid="gpushowner",
        recipient_alias="fam-push-route",
        kind="text",
        body="hi family",
        origin_backend_kind="pager",
    )

    assert len(fcm.calls) == 1
    tokens, data = fcm.calls[0]
    assert tokens == ["tok_gpushmem"]
    assert data["convKey"] == conv.convKey
    assert data["senderAlias"] == "gpushowner"
    assert data["title"] == "Family"
    assert data["url"] == "/chat/fam-push-route"
    assert data["groupMsgId"]  # non-empty, shared with the pager copy's wireId
