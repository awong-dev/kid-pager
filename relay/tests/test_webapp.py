"""Tests for the `deliver()` payload contract of `app/backends/webapp.py`, with `FCMClient` faked
and message/delivery rows built in memory."""

from __future__ import annotations

import pytest

from app.backends.webapp import WebappBackend, push_alert
from app.store import conversations as conversations_store
from app.store import push_tokens as push_tokens_store
from app.store import users as users_store
from app.store.backends import Backend as BackendRow
from app.store.messages import Delivery, Message


class _RecordingFCMClient:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], dict[str, str]]] = []

    def send_data(self, tokens: list[str], data: dict[str, str]) -> None:
        self.calls.append((tokens, data))


def _message(**overrides: object) -> Message:
    defaults: dict[str, object] = {
        "id": "msg1",
        "seq": 1,
        "convKey": "alice_bob",
        "uids": ["alice", "bob"],
        "senderUid": "alice",
        "recipientUid": "bob",
        "kind": "text",
        "body": "hello there, this is a rather long message body meant to exercise truncation" * 2,
        "ts": 1234,
        "deliveries": {"be1": Delivery(kind="webapp", state="queued")},
    }
    defaults.update(overrides)
    return Message.model_validate(defaults)


def test_deliver_sends_the_full_payload_contract():
    users_store.create_user(uid="alice", alias="alice-alias", display_name="Alice")
    push_tokens_store.add_token("bob", "tok_bob")

    fcm = _RecordingFCMClient()
    backend = WebappBackend(fcm_client=fcm)
    msg = _message()
    backend.deliver(msg, msg.deliveries["be1"], BackendRow(id="be1", kind="webapp"))

    assert len(fcm.calls) == 1
    tokens, data = fcm.calls[0]
    assert tokens == ["tok_bob"]
    assert set(data.keys()) == {
        "kind",
        "convKey",
        "id",
        "senderUid",
        "senderAlias",
        "title",
        "body",
        "url",
    }
    assert all(isinstance(v, str) for v in data.values())
    assert data["kind"] == "message"
    assert data["convKey"] == "alice_bob"
    assert data["id"] == "msg1"
    assert data["senderUid"] == "alice"
    assert data["senderAlias"] == "alice-alias"
    assert data["title"] == "alice-alias"
    assert data["body"] == (msg.body or "")[:120]
    assert len(data["body"]) == 120
    assert data["url"] == "/chat/alice-alias"


def test_deliver_falls_back_to_uid_when_sender_has_no_user_doc():
    # No users_store.create_user for "ghost" (a deleted sender): the push still goes out,
    # with the raw uid standing in for the alias.
    push_tokens_store.add_token("bob", "tok_bob")

    fcm = _RecordingFCMClient()
    backend = WebappBackend(fcm_client=fcm)
    msg = _message(senderUid="ghost", body="hi")
    backend.deliver(msg, msg.deliveries["be1"], BackendRow(id="be1", kind="webapp"))

    assert len(fcm.calls) == 1
    _, data = fcm.calls[0]
    assert data["senderAlias"] == "ghost"
    assert data["title"] == "ghost"
    assert data["url"] == "/chat/ghost"


def test_deliver_with_no_tokens_does_not_call_fcm():
    users_store.create_user(uid="alice", alias="alice-alias", display_name="Alice")

    fcm = _RecordingFCMClient()
    backend = WebappBackend(fcm_client=fcm)
    msg = _message(recipientUid="nobody")
    backend.deliver(msg, msg.deliveries["be1"], BackendRow(id="be1", kind="webapp"))

    assert fcm.calls == []


def test_deliver_group_copy_uses_group_name_and_alias_and_collapse_key():
    users_store.create_user(uid="galice", alias="galice-alias", display_name="G Alice")
    users_store.create_user(uid="gbob", alias="gbob-alias", display_name="G Bob")
    conv = conversations_store.create_group(
        name="Family", alias="fam-push", member_uids=["galice", "gbob"], created_by="galice"
    )
    push_tokens_store.add_token("gbob", "tok_gbob")

    fcm = _RecordingFCMClient()
    backend = WebappBackend(fcm_client=fcm)
    msg = _message(
        convKey=conv.convKey,
        senderUid="galice",
        recipientUid="gbob",
        uids=["galice", "gbob"],
        groupMsgId="gm_push1",
        senderAlias="galice-alias",
    )
    backend.deliver(msg, msg.deliveries["be1"], BackendRow(id="be1", kind="webapp"))

    assert len(fcm.calls) == 1
    tokens, data = fcm.calls[0]
    assert tokens == ["tok_gbob"]
    assert set(data.keys()) == {
        "kind",
        "convKey",
        "id",
        "senderUid",
        "senderAlias",
        "title",
        "body",
        "url",
        "groupMsgId",
    }
    assert data["kind"] == "message"
    assert data["convKey"] == conv.convKey
    assert data["senderUid"] == "galice"
    # denormalised straight off the message, no `users/{uid}` lookup needed.
    assert data["senderAlias"] == "galice-alias"
    assert data["title"] == "Family"
    assert data["url"] == f"/chat/{conv.alias}"
    assert data["groupMsgId"] == "gm_push1"


def test_push_alert_new_conversation_sends_exact_data_map():
    users_store.create_user(
        uid="fam1admin", alias="fam1admin", display_name="Admin", role="admin", family_id="fam1"
    )
    push_tokens_store.add_token("fam1admin", "tok_admin")

    fcm = _RecordingFCMClient()
    push_alert(
        "fam1",
        {
            "kind": "new_conversation",
            "id": "alert1",
            "subjectAlias": "kid",
            "peerAlias": "peer",
            "preview": "hey there",
        },
        fcm_client=fcm,
    )

    assert len(fcm.calls) == 1
    tokens, data = fcm.calls[0]
    assert tokens == ["tok_admin"]
    assert data == {
        "kind": "alert",
        "alertKind": "new_conversation",
        "id": "alert1",
        "title": "New chat: @kid ↔ @peer",
        "body": "hey there",
        "url": "/family/alerts",
    }
    assert all(isinstance(v, str) for v in data.values())


@pytest.mark.parametrize(
    ("alert", "title", "body"),
    [
        pytest.param(
            {"kind": "sms_unknown", "id": "alert2", "subjectAlias": "kid2", "preview": "x" * 200},
            "Text from an unknown number for @kid2",
            "x" * 120,
            id="sms-unknown-truncated-body",
        ),
        pytest.param(
            {"kind": "contact_request", "id": "alert3", "subjectAlias": "kid3"},
            "Contact request from @kid3's pager",
            "",
            id="contact-request-empty-preview",
        ),
    ],
)
def test_push_alert_title_and_body_per_kind(alert: dict, title: str, body: str):
    users_store.create_user(
        uid="fam2admin", alias="fam2admin", display_name="Admin", role="admin", family_id="fam2"
    )
    push_tokens_store.add_token("fam2admin", "tok_admin2")

    fcm = _RecordingFCMClient()
    push_alert("fam2", alert, fcm_client=fcm)

    _, data = fcm.calls[0]
    assert data["title"] == title
    assert data["body"] == body


def _alert_push(family: str, *users: tuple[str, str, str | None, bool]) -> _RecordingFCMClient:
    for uid, role, fam, notify in users:
        users_store.create_user(
            uid=uid, alias=uid, display_name=uid, role=role, family_id=fam
        )
        if not notify:
            users_store.update_user(uid, notify_alerts=False)
        push_tokens_store.add_token(uid, f"tok_{uid}")
    fcm = _RecordingFCMClient()
    push_alert(
        family,
        {"kind": "contact_request", "id": "alertx", "subjectAlias": "kidx"},
        fcm_client=fcm,
    )
    return fcm


@pytest.mark.parametrize(
    ("users", "tokens"),
    [
        pytest.param([("fam4admin", "admin", "fam4", False)], [], id="admin-opted-out"),
        pytest.param([("fam5super", "super", "fam4", True)], [["tok_fam5super"]], id="super-of-the-family"),
        pytest.param([("fam6super", "super", "other6", True)], [], id="super-of-another-family"),
        pytest.param([("fam7member", "member", "fam4", True)], [], id="member"),
        pytest.param([("fam8super", "super", "fam4", False)], [], id="super-opted-out"),
    ],
)
def test_push_alert_recipients(users: list[tuple[str, str, str | None, bool]], tokens: list[list[str]]):
    fcm = _alert_push("fam4", *users)
    assert [t for t, _ in fcm.calls] == tokens
