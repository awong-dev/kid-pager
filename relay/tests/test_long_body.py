"""Long messages: the `mb` status key and the per-recipient split in `Routing`."""

# ruff: noqa: F811 -- fixtures imported from tests/hooks_world.py
from __future__ import annotations

import json
import time

import pytest

from app.ingest import Ingest
from app.routing import Routing
from app.store import backends as backends_store
from app.store import conversations as conversations_store
from app.store import devices as devices_store
from app.store import messages as messages_store
from tests.conftest import offline_status_payload, online_status_payload, status_topic
from tests.fake_transport import FakeBrokerClient
from tests.hooks_world import (
    FakeHooks,
    add_hook,
    befriend,
    fake_hooks,  # noqa: F401 -- fixture
    make_user,
    pager_for,
    web_send,
)

LONG = ("word " * 120).strip()  # 599 chars, > 320 bytes, < 1024


@pytest.fixture
def routing() -> Routing:
    return Routing(FakeBrokerClient())


def _report_mb(device_id: str, mb: int | None, *, fw: str = "1.0.0") -> None:
    broker = FakeBrokerClient()
    ingest = Ingest(broker, Routing(broker))
    extra = {} if mb is None else {"mb": mb}
    ingest.handle_status(
        status_topic(device_id),
        online_status_payload(
            f"s_{int(time.time() * 1000) % 2**32:08x}", fw=fw, **extra
        ),
    )


def _thread(a: str, b: str) -> list:
    return messages_store.list_thread(messages_store.conv_key(a, b), limit=200)


@pytest.fixture
def world():
    make_user("alice", "alice", "fam1")
    make_user("kid", "kid", "fam1")
    make_user("mom", "mom", "fam1")
    befriend("alice", "kid")
    befriend("alice", "mom")
    befriend("kid", "mom")


def test_mb_is_stored_kept_by_later_statuses_and_cleared_on_a_firmware_change(world):
    pager_for("kid", "pgr-1")
    _report_mb("pgr-1", 1024, fw="2.0.0")
    assert devices_store.get_device("pgr-1").status.mb == 1024
    # same firmware, `mb` omitted: keep
    _report_mb("pgr-1", None, fw="2.0.0")
    assert devices_store.get_device("pgr-1").status.mb == 1024
    # an offline/LWT status carries neither `mb` nor `fw`: keep
    broker = FakeBrokerClient()
    Ingest(broker, Routing(broker)).handle_status(
        status_topic("pgr-1"), offline_status_payload("s_00000001")
    )
    assert devices_store.get_device("pgr-1").status.mb == 1024
    # a changed firmware that does not report it: clear
    _report_mb("pgr-1", None, fw="1.9.0")
    assert devices_store.get_device("pgr-1").status.mb is None


def test_short_message_is_untouched_and_does_no_extra_lookup(
    world, routing, monkeypatch
):
    pager_for("kid", "pgr-1")
    calls: list[str] = []
    real = Routing._recipient_cap
    monkeypatch.setattr(
        Routing, "_recipient_cap", staticmethod(lambda u: (calls.append(u), real(u))[1])
    )
    r = web_send(routing, "alice", "kid", "y" * 160, wire_id="w1")
    assert [m.body for m in r.messages] == ["y" * 160] and calls == []


def test_legacy_pager_gets_160_code_point_parts_in_seq_order(world, routing):
    pager_for("kid", "pgr-1")  # never reported mb
    r = web_send(routing, "alice", "kid", "ñ" * 400, wire_id="w1", ts=1_700_000_000)
    assert [len(m.body) for m in r.messages] == [160, 160, 80]
    assert all(len(m.body.encode()) <= 320 for m in r.messages)
    assert [m.wireId for m in r.messages] == ["w1", "w1~1", "w1~2"]
    seqs = [m.seq for m in r.messages]
    assert seqs == sorted(seqs) and len(set(seqs)) == 3
    assert {m.ts for m in r.messages} == {1_700_000_000}
    assert [m.body for m in _thread("alice", "kid")] == [m.body for m in r.messages]


def test_mb_pager_gets_1024_byte_parts(world, routing):
    pager_for("kid", "pgr-1")
    _report_mb("pgr-1", 1024)
    r = web_send(routing, "alice", "kid", "ab " * 1000)
    assert len(r.messages) == 3
    assert all(len(m.body.encode()) <= 1024 for m in r.messages)
    assert "".join(m.body for m in r.messages).replace(" ", "") == ("ab" * 1000)
    # 600 chars: one message for an `mb` pager, two for a legacy one
    one = web_send(routing, "alice", "kid", LONG)
    assert [m.body for m in one.messages] == [LONG]


def test_recipient_without_a_pager_or_with_only_a_revoked_one_gets_1024(world, routing):
    r = web_send(routing, "alice", "mom", LONG)
    assert [m.body for m in r.messages] == [LONG]
    pager_for("kid", "pgr-9")
    assert (
        len(web_send(routing, "alice", "kid", LONG).messages) == 4
    )  # legacy pager: 160 cp
    devices_store.revoke_device("pgr-9")
    assert [m.body for m in web_send(routing, "alice", "kid", LONG).messages] == [LONG]


def test_two_pagers_use_the_smaller_cap(world, routing):
    pager_for("kid", "pgr-1")
    _report_mb("pgr-1", 1024)
    pager_for("kid", "pgr-2")  # second pager, legacy
    assert len(web_send(routing, "alice", "kid", LONG).messages) == 4


def test_disabled_pager_backend_does_not_set_the_cap(world, routing):
    pager_for("kid", "pgr-1")
    (pager,) = [b for b in backends_store.list_backends("kid") if b.kind == "pager"]
    backends_store.update_backend("kid", pager.id, enabled=False)
    assert [m.body for m in web_send(routing, "alice", "kid", LONG).messages] == [LONG]


def test_resent_wire_id_dedups_every_part(world, routing):
    pager_for("kid", "pgr-1")
    first = web_send(routing, "alice", "kid", "z" * 400, wire_id="w7")
    again = web_send(routing, "alice", "kid", "z" * 400, wire_id="w7")
    assert len(first.messages) == 3 and again.messages == []
    assert len(_thread("alice", "kid")) == 3


def test_partial_fanout_is_repaired_by_a_resend(world, routing):
    pager_for("kid", "pgr-1")
    full = web_send(routing, "alice", "kid", "z" * 400, wire_id="w8")
    from app.db.firestore import get_db

    # lose part 2 (as a crash between parts would)
    lost = full.messages[2]
    get_db().collection("messages").document(lost.id).delete()
    get_db().collection("wireIds").document("w8~2_kid").delete()
    again = web_send(routing, "alice", "kid", "z" * 400, wire_id="w8")
    assert [m.wireId for m in again.messages] == ["w8~2"]


def test_group_copies_use_the_smallest_cap_and_identical_parts(world, routing):
    pager_for("kid", "pgr-1")  # legacy
    pager_for("mom", "pgr-2")
    _report_mb("pgr-2", 1024)
    group = conversations_store.create_group(
        created_by="alice", name="Fam", alias="fam", member_uids=["alice", "kid", "mom"]
    )
    r = web_send(routing, "alice", "fam", "q" * 400, wire_id="g1")
    assert len(r.messages) == 6  # 3 parts x 2 members
    by_member: dict[str, list] = {}
    for m in r.messages:
        by_member.setdefault(m.recipientUid, []).append(m)
    assert [m.body for m in by_member["kid"]] == [m.body for m in by_member["mom"]]
    assert [len(m.body) for m in by_member["kid"]] == [160, 160, 80]
    for kid, mom in zip(by_member["kid"], by_member["mom"], strict=True):
        assert kid.seq == mom.seq and kid.groupMsgId == mom.groupMsgId
    assert [m.groupMsgId for m in by_member["kid"]] == ["g1", "g1~1", "g1~2"]
    assert all(m.convKey == group.convKey for m in r.messages)
    again = web_send(routing, "alice", "fam", "q" * 400, wire_id="g1")
    assert again.messages == []


def test_cjk_and_emoji_split_on_code_points(world, routing):
    pager_for("kid", "pgr-1")
    _report_mb("pgr-1", 1024)
    cjk = "你" * 1000
    emoji = "😀" * 600
    for text in (cjk, emoji):
        r = web_send(routing, "alice", "kid", text)
        assert all(len(m.body.encode()) <= 1024 for m in r.messages)
        assert "".join(m.body for m in r.messages) == text
    assert [len(m.body) for m in web_send(routing, "alice", "kid", cjk).messages] == [
        341,
        341,
        318,
    ]


def test_hooks_see_the_whole_body_and_the_split_follows_a_rewrite(
    world, routing, fake_hooks: FakeHooks
):
    pager_for("kid", "pgr-1")
    add_hook()
    fake_hooks.respond["h1.test"] = {"action": "replace", "body": "r" * 400}
    r = web_send(routing, "alice", "kid", "x" * 500)
    assert fake_hooks.calls[0].payload["message"]["body"] == "x" * 500
    assert len(fake_hooks.calls) == 1
    assert [len(m.body) for m in r.messages] == [160, 160, 80]
    assert all(set(m.body) == {"r"} for m in r.messages)


def test_web_single_message_cap_is_1024_bytes(world):
    from fastapi.testclient import TestClient
    from firebase_admin import auth as fb_auth

    from app.main import create_app
    from tests.firebase_test_utils import auth_header
    from tests.test_admin import make_settings

    acct = fb_auth.create_user(email="w@example.com")
    make_user(acct.uid, "wendy", "fam1")
    fb_auth.set_custom_user_claims(acct.uid, {"role": "member", "fam": "fam1"})
    befriend(acct.uid, "mom")
    app = create_app(settings=make_settings(), broker_client=FakeBrokerClient())
    with TestClient(app) as c:
        h = auth_header(acct.uid)
        ok = c.post(
            "/api/conversations/mom/messages", json={"body": "é" * 512}, headers=h
        )
        assert ok.status_code == 201
        big = c.post(
            "/api/conversations/mom/messages", json={"body": "é" * 513}, headers=h
        )
        assert big.status_code == 400 and "1024" in big.json()["detail"]
        # more than 160 code points is fine
        assert (
            c.post(
                "/api/conversations/mom/messages", json={"body": "a" * 700}, headers=h
            ).status_code
            == 201
        )


def test_up_envelope_cap_1024_bytes_through_ingest(world):
    pager_for("kid", "pgr-1")
    broker = FakeBrokerClient()
    ingest = Ingest(broker, Routing(broker))
    for i, (body, n) in enumerate(
        (("a" * 1024, 1), ("a" * 1025, 1))
    ):  # n: messages stored so far
        ingest.handle_up(
            "pager/pgr-1/up",
            json.dumps(
                {
                    "v": 1,
                    "id": f"u_big{i}",
                    "ts": int(time.time()),
                    "from": "kid",
                    "to": "mom",
                    "body": body,
                    "ack": None,
                }
            ).encode(),
        )
        assert len(_thread("kid", "mom")) == n
