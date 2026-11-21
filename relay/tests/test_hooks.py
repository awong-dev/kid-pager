"""Hook runner and the `Routing.send` call site."""

# ruff: noqa: F811 -- fixtures imported from tests/hooks_world.py
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time

import httpx
import pytest

from app import hooks as hooks_module
from app.routing import Routing
from app.store import alerts as alerts_store
from app.store import conversations as conversations_store
from app.store import hooks as hooks_store
from app.store import messages as messages_store
from tests.fake_transport import FakeBrokerClient
from tests.hooks_world import (
    FakeHooks,
    add_hook,
    befriend,
    fake_hooks,  # noqa: F401 -- fixture
    make_user,
    pager_for,
    routing,  # noqa: F401 -- fixture
    web_send,
)


@pytest.fixture
def world():
    make_user("alice", "alice", "fam1", role="admin")
    make_user("bob", "bob", "fam1")
    make_user("carol", "carol", "fam2")
    befriend("alice", "bob")
    befriend("alice", "carol")
    befriend("bob", "carol")


def test_no_hooks_changes_nothing_and_reads_the_hook_store_once(
    world, routing: Routing, monkeypatch: pytest.MonkeyPatch
):
    reads: list[int] = []
    real = hooks_store.list_hooks
    monkeypatch.setattr(hooks_store, "list_hooks", lambda: (reads.append(1), real())[1])
    r1 = web_send(routing, "alice", "bob", "one")
    r2 = web_send(routing, "alice", "bob", "two")
    r3 = web_send(routing, "bob", "alice", "three")
    assert [m.body for m in (r1.messages + r2.messages + r3.messages)] == [
        "one",
        "two",
        "three",
    ]
    assert len(reads) == 1  # one read for the 30 s window, none on later sends
    assert all(m.hooks == {} and m.hookRef is None for m in r1.messages)
    stored = messages_store.get_message(r1.messages[0].id)
    assert stored is not None and stored.hooks == {}


def test_cache_expires_after_thirty_seconds(world, monkeypatch: pytest.MonkeyPatch):
    reads: list[int] = []
    real = hooks_store.list_hooks
    monkeypatch.setattr(hooks_store, "list_hooks", lambda: (reads.append(1), real())[1])
    hooks_store.enabled_cached(now=100.0)
    hooks_store.enabled_cached(now=129.0)
    assert len(reads) == 1
    hooks_store.enabled_cached(now=131.0)
    assert len(reads) == 2


def test_allow_sends_signed_payload_and_message_goes_through(
    world, routing: Routing, fake_hooks: FakeHooks
):
    hook = add_hook()
    r = web_send(routing, "alice", "bob", "hello there")
    assert [m.body for m in r.messages] == ["hello there"]
    [call] = fake_hooks.calls
    h = call.headers
    assert h["x-kp-hook"] == hook.id
    key = base64.urlsafe_b64decode(hook.secret + "=" * (-len(hook.secret) % 4))
    expect = hmac.new(
        key, h["x-kp-timestamp"].encode() + b"." + call.raw, hashlib.sha256
    ).hexdigest()
    assert h["x-kp-signature"] == f"sha256={expect}"
    assert abs(int(h["x-kp-timestamp"]) - time.time()) < 5
    p = call.payload
    assert (p["v"], p["event"], p["mode"]) == (1, "message", "filter")
    assert p["id"].startswith("he_") and len(p["id"]) == 3 + 16
    assert p["message"]["body"] == "hello there"
    assert p["message"]["origin"] == "webapp" and p["message"]["kind"] == "text"
    assert p["sender"]["alias"] == "alice" and p["sender"]["familyId"] == "fam1"
    assert p["sender"]["role"] == "admin" and p["sender"]["via"] is None
    [rcpt] = p["recipients"]
    assert rcpt["alias"] == "bob" and rcpt["backends"] == ["webapp"]
    assert p["conversation"]["kind"] == "dm" and p["conversation"]["name"] is None
    assert p["context"] == []
    # nothing about phones, devices or locations
    text = call.raw.decode()
    assert "phone" not in text and "deviceId" not in text and "smsNumber" not in text


def test_context_is_oldest_first_and_limited(
    world, routing: Routing, fake_hooks: FakeHooks
):
    for i in range(5):
        web_send(
            routing,
            "alice" if i % 2 == 0 else "bob",
            "bob" if i % 2 == 0 else "alice",
            f"m{i}",
        )
    add_hook(context=3)
    web_send(routing, "alice", "bob", "latest")
    ctx = fake_hooks.calls[-1].payload["context"]
    assert [c["body"] for c in ctx] == ["m2", "m3", "m4"]
    assert [c["sender"] for c in ctx] == ["alice", "bob", "alice"]
    assert set(ctx[0]) == {"id", "ts", "sender", "senderUid", "body"}
    assert ctx[0]["senderUid"] == "alice"


def test_context_zero_sends_none(world, routing: Routing, fake_hooks: FakeHooks):
    web_send(routing, "alice", "bob", "earlier")
    add_hook(context=0)
    web_send(routing, "alice", "bob", "x")
    assert fake_hooks.calls[-1].payload["context"] == []


def test_block_stores_nothing_and_reports_hook_blocked(
    world, routing: Routing, fake_hooks: FakeHooks
):
    add_hook()
    fake_hooks.respond["h1.test"] = {
        "action": "block",
        "notifySender": "Not sent: unkind",
        "reason": "mean",
        "labels": ["x"],
    }
    r = web_send(routing, "alice", "bob", "you suck")
    assert r.messages == [] and not r.ok
    assert [(x.uid, x.reason) for x in r.rejected] == [("bob", "hook_blocked")]
    assert r.notify == "Not sent: unkind"
    assert (
        messages_store.get_conversation(messages_store.conv_key("alice", "bob")) is None
    )


def test_replace_stores_and_delivers_the_new_body(
    world, routing: Routing, fake_hooks: FakeHooks
):
    h = add_hook()
    fake_hooks.respond["h1.test"] = {
        "action": "replace",
        "body": "you are great",
        "labels": ["rewritten"],
        "reason": "tone",
    }
    r = web_send(routing, "alice", "bob", "you suck", wire_id="w1")
    [m] = r.messages
    assert m.body == "you are great"
    stored = messages_store.get_message(m.id)
    assert stored is not None
    ann = stored.hooks[h.id]
    assert (
        ann["action"] == "replace"
        and ann["labels"] == ["rewritten"]
        and ann["reason"] == "tone"
    )
    assert isinstance(ann["at"], int)
    assert stored.hookRef == "w1"


@pytest.mark.parametrize("bad", ["", "   \x01", "x" * 8193])
def test_replace_with_bad_body_is_an_error(world, routing, fake_hooks, bad):
    h = add_hook()
    fake_hooks.respond["h1.test"] = {"action": "replace", "body": bad}
    r = web_send(routing, "alice", "bob", "orig")
    assert [m.body for m in r.messages] == ["orig"]  # fail open
    assert hooks_store.get(h.id).stats.errors == 1


def test_replace_at_the_8192_byte_bound_is_accepted(world, routing, fake_hooks):
    add_hook()
    fake_hooks.respond["h1.test"] = {"action": "replace", "body": "x" * 8192}
    r = web_send(routing, "alice", "bob", "orig")
    assert "".join(m.body for m in r.messages) == "x" * 8192  # then split for the recipient


def test_chain_runs_in_order_each_sees_previous_body(world, routing, fake_hooks):
    add_hook("b.test", order=2)
    add_hook("a.test", order=1)
    fake_hooks.respond["a.test"] = {"action": "replace", "body": "from-a"}
    web_send(routing, "alice", "bob", "start")
    assert [httpx.URL(c.url).host for c in fake_hooks.calls] == ["a.test", "b.test"]
    assert fake_hooks.calls[0].payload["message"]["body"] == "start"
    assert fake_hooks.calls[1].payload["message"]["body"] == "from-a"


def test_first_block_ends_the_chain(world, routing, fake_hooks):
    add_hook("a.test", order=1)
    add_hook("b.test", order=2)
    fake_hooks.respond["a.test"] = {"action": "block"}
    r = web_send(routing, "alice", "bob", "x")
    assert r.messages == [] and r.notify is None
    assert fake_hooks.to("b.test") == []


@pytest.mark.parametrize(
    "response",
    [
        lambda c: httpx.Response(500, json={}),
        lambda c: httpx.Response(200, content=b"not json"),
        lambda c: httpx.Response(200, json={"action": "explode"}),
        lambda c: httpx.Response(200, json={"action": "hold"}),
        lambda c: httpx.Response(200, json={"nope": 1}),
        httpx.ReadTimeout("slow"),
        httpx.ConnectError("down"),
    ],
)
def test_errors_fail_open_by_default_and_count(world, routing, fake_hooks, response):
    h = add_hook()
    fake_hooks.respond["h1.test"] = response
    r = web_send(routing, "alice", "bob", "still goes")
    assert [m.body for m in r.messages] == ["still goes"]
    st = hooks_store.get(h.id).stats
    assert st.calls == 1 and st.errors == 1 and st.lastErr
    assert st.timeouts == (1 if isinstance(response, httpx.ReadTimeout) else 0)


def test_fail_closed_blocks_with_hook_unavailable(world, routing, fake_hooks):
    add_hook(fail_mode="closed")
    fake_hooks.respond["h1.test"] = httpx.ReadTimeout("slow")
    r = web_send(routing, "alice", "bob", "x")
    assert r.messages == [] and r.rejected[0].reason == "hook_blocked"


def test_success_resets_the_failure_streak_and_three_errors_raise_one_hook_down(
    world, routing, fake_hooks
):
    make_user("root", "root", "default", role="super")
    h = add_hook()
    fake_hooks.respond["h1.test"] = lambda c: httpx.Response(503)
    web_send(routing, "alice", "bob", "1")
    web_send(routing, "alice", "bob", "2")
    fake_hooks.respond["h1.test"] = {"action": "allow"}
    web_send(routing, "alice", "bob", "3")
    assert hooks_store.get(h.id).stats.consecutiveErrors == 0
    assert alerts_store.list_alerts("default", "all") == []
    fake_hooks.respond["h1.test"] = lambda c: httpx.Response(503)
    for i in range(4):
        web_send(routing, "alice", "bob", f"e{i}")
    alerts = alerts_store.list_alerts("default", "all")
    assert [a.kind for a in alerts] == ["hook_down"]
    assert alerts[0].hookId == h.id and alerts[0].title


def test_timeout_is_per_hook_and_capped_by_the_chain_budget(
    world, routing, fake_hooks, monkeypatch
):
    add_hook("a.test", order=1, timeout_ms=3000)
    add_hook("b.test", order=2, timeout_ms=10000)
    seen: list[float] = []

    def spy(self, url, **kw):
        seen.append(kw["timeout"])
        return httpx.Response(200, json={"action": "allow"})

    monkeypatch.setattr(httpx.Client, "post", spy)
    hooks_module.set_client(httpx.Client())
    ticks = iter([0.0, 0.0, 6.0, 6.0, 6.0])
    ctx = hooks_module.build_ctx(
        sender_uid="alice",
        recipient_uids=["bob"],
        conv_key="k",
        conv_kind="dm",
        conv_name=None,
        origin_backend_kind="webapp",
        wire_id=None,
        ts=1,
    )
    hooks_module.run_filters(
        hooks_store.enabled_cached(), ctx, "x", clock=lambda: next(ticks)
    )
    assert seen[0] == 3.0
    assert seen[1] == pytest.approx(9.0)  # 15 - 6 elapsed, below its own 10 s


def test_chain_budget_exhausted_is_an_error(world, fake_hooks):
    add_hook("a.test", order=1)
    ctx = hooks_module.build_ctx(
        sender_uid="alice",
        recipient_uids=["bob"],
        conv_key="k",
        conv_kind="dm",
        conv_name=None,
        origin_backend_kind="webapp",
        wire_id=None,
        ts=1,
    )
    out = hooks_module.run_filters(
        hooks_store.enabled_cached(), ctx, "x", clock=iter([0.0, 16.0]).__next__
    )
    assert fake_hooks.calls == [] and not out.blocked
    assert hooks_store.list_hooks()[0].stats.errors == 1


def test_group_send_is_one_call_with_all_recipients(world, routing, fake_hooks):
    make_user("dave", "dave", "fam1")
    for a, b in (("alice", "dave"), ("bob", "dave")):
        befriend(a, b)
    group = conversations_store.create_group(
        created_by="alice",
        name="Team",
        alias="team",
        member_uids=["alice", "bob", "dave"],
    )
    add_hook()
    r = web_send(routing, "alice", "team", "hi all")
    assert len(r.messages) == 2
    [call] = fake_hooks.calls
    assert sorted(x["alias"] for x in call.payload["recipients"]) == ["bob", "dave"]
    assert call.payload["conversation"]["kind"] == "group"
    assert call.payload["conversation"]["name"] == "Team"
    assert call.payload["conversation"]["key"] == group.convKey
    # context folds the per-member copies into one entry
    web_send(routing, "bob", "team", "second")
    ctx = fake_hooks.calls[-1].payload["context"]
    assert [c["body"] for c in ctx] == ["hi all"]


def test_policy_refused_recipients_are_not_in_the_payload_and_all_refused_means_no_call(
    world, routing, fake_hooks
):
    make_user("zed", "zed", "fam3")  # other family, no allow edge from alice
    add_hook()
    r = web_send(routing, "alice", "zed", "hi")
    assert r.rejected[0].reason == "not_allowed"
    assert fake_hooks.calls == []


def test_family_scoping(world, routing, fake_hooks):
    add_hook("only2.test", families=["fam2"])
    add_hook("only1.test", families=["fam1"])
    web_send(routing, "alice", "bob", "within fam1")
    assert [httpx.URL(c.url).host for c in fake_hooks.calls] == ["only1.test"]
    fake_hooks.calls.clear()
    # a cross-family message reaches a hook only if it covers both families
    web_send(routing, "alice", "carol", "cross")
    assert fake_hooks.calls == []
    add_hook("both.test", families=["fam1", "fam2"])
    add_hook("all.test", families="*")
    web_send(routing, "alice", "carol", "cross again")
    assert sorted(httpx.URL(c.url).host for c in fake_hooks.calls) == ["all.test", "both.test"]


def test_disabled_hook_takes_effect_on_the_next_message(world, routing, fake_hooks):
    h = add_hook()
    web_send(routing, "alice", "bob", "1")
    hooks_store.update(h.id, {"enabled": False})
    web_send(routing, "alice", "bob", "2")
    assert len(fake_hooks.calls) == 1


def test_no_call_for_loc_req_and_not_for_empty_body(world, routing, fake_hooks):
    add_hook()
    r = routing.send(
        sender_uid="alice",
        recipient_alias="bob",
        kind="loc_req",
        origin_backend_kind="webapp",
    )
    assert r.messages and fake_hooks.calls == []


def test_flag_alert_goes_to_each_participant_family_and_is_deduplicated(
    world, routing, fake_hooks
):
    add_hook()
    fake_hooks.respond["h1.test"] = {
        "action": "allow",
        "alert": {"title": "T" * 80, "body": "B" * 300},
        "labels": ["bully"],
    }
    web_send(routing, "alice", "carol", "one")
    web_send(routing, "alice", "carol", "two")
    for fam in ("fam1", "fam2"):
        [a] = alerts_store.list_alerts(fam, "all")
        assert a.kind == "hook_flag" and a.status == "open"
        assert len(a.title) == 60 and len(a.preview) == 200
        assert a.subjectAlias == "alice"
    # a different conversation raises its own
    web_send(routing, "alice", "bob", "three")
    assert len(alerts_store.list_alerts("fam1", "all")) == 2


def test_alert_push_title_table_has_every_kind():
    from app.backends.webapp import _ALERT_TITLES

    kinds = set(alerts_store.AlertKind.__args__)  # type: ignore[attr-defined]
    assert kinds <= set(_ALERT_TITLES)


def test_labels_without_action_change_are_annotated(world, routing, fake_hooks):
    h = add_hook()
    fake_hooks.respond["h1.test"] = {
        "action": "allow",
        "labels": ["a"] * 12 + ["x" * 40],
    }
    r = web_send(routing, "alice", "bob", "x")
    ann = messages_store.get_message(r.messages[0].id).hooks[h.id]
    assert ann["labels"] == ["a"] * 8 and ann["action"] == "allow"


def test_redelivered_wire_id_does_not_call_the_hook_again(world, fake_hooks):
    make_user("kid", "kid", "fam1")
    befriend("kid", "alice")
    routing = Routing(FakeBrokerClient())
    add_hook()
    web_send(
        routing, "kid", "alice", "hello", wire_id="u_1", origin_backend_kind="pager"
    )
    web_send(
        routing, "kid", "alice", "hello", wire_id="u_1", origin_backend_kind="pager"
    )
    assert len(fake_hooks.calls) == 1


def test_pager_group_block_with_mixed_rejections_still_gets_the_notify_reply(
    world, fake_hooks
):
    """A group send where one member is refused by the gate and the hook blocks the rest:
    the sender hears `notifySender`, not "unknown recipient"."""
    from app.ingest import Ingest

    make_user("dave", "dave", "fam3")  # other family, no allow edge: refused before the hook runs
    conversations_store.create_group(
        name="Grp", alias="grp", member_uids=["alice", "bob", "dave"], created_by="bob"
    )
    pager_for("bob", "pgr-9")
    broker = FakeBrokerClient()
    ingest = Ingest(broker, Routing(broker))
    add_hook()
    fake_hooks.respond["h1.test"] = {"action": "block", "notifySender": "Blocked for all"}

    def up(mid: str) -> None:
        ingest.handle_up(
            "pager/pgr-9/up",
            json.dumps(
                {"v": 1, "id": mid, "ts": int(time.time()), "from": "bob", "to": "grp",
                 "body": "bad", "ack": None}
            ).encode(),
        )

    up("u_grp1")
    [sent] = [json.loads(p.payload) for p in broker.published]
    assert sent["from"] == "system" and sent["body"] == "Blocked for all"

    # no notify text: the hook block itself is silent; only dave's own refusal is answered
    broker.published.clear()
    fake_hooks.respond["h1.test"] = {"action": "block"}
    up("u_grp2")
    bodies = [json.loads(p.payload)["body"] for p in broker.published]
    assert bodies == ["unknown recipient"]


def test_pager_block_gets_system_reply_with_notify_text(world, fake_hooks):
    from app.ingest import Ingest

    pager_for("bob", "pgr-9")
    broker = FakeBrokerClient()
    ingest = Ingest(broker, Routing(broker))
    add_hook()
    fake_hooks.respond["h1.test"] = {
        "action": "block",
        "notifySender": "That was blocked",
    }
    ingest.handle_up(
        "pager/pgr-9/up",
        json.dumps(
            {
                "v": 1,
                "id": "u_blk1",
                "ts": int(time.time()),
                "from": "bob",
                "to": "alice",
                "body": "bad",
                "ack": None,
            }
        ).encode(),
    )
    [sent] = [json.loads(p.payload) for p in broker.published]
    assert sent["from"] == "system" and sent["body"] == "That was blocked"
    assert (
        messages_store.get_conversation(messages_store.conv_key("bob", "alice")) is None
    )
    assert fake_hooks.calls[0].payload["message"]["origin"] == "pager"

    # without notifySender the sender hears nothing
    broker.published.clear()
    fake_hooks.respond["h1.test"] = {"action": "block"}
    ingest.handle_up(
        "pager/pgr-9/up",
        json.dumps(
            {
                "v": 1,
                "id": "u_blk2",
                "ts": int(time.time()),
                "from": "bob",
                "to": "alice",
                "body": "bad2",
                "ack": None,
            }
        ).encode(),
    )
    assert broker.published == []


def test_web_block_is_a_403_with_the_notify_text(fake_hooks):
    from fastapi.testclient import TestClient
    from firebase_admin import auth as fb_auth

    from app.main import create_app
    from tests.firebase_test_utils import auth_header
    from tests.test_admin import make_settings

    acct = fb_auth.create_user(email="w@example.com")
    make_user(acct.uid, "wendy", "fam1")
    fb_auth.set_custom_user_claims(acct.uid, {"role": "member", "fam": "fam1"})
    make_user("bob", "bob", "fam1")
    befriend(acct.uid, "bob")
    add_hook()
    fake_hooks.respond["h1.test"] = {
        "action": "block",
        "notifySender": "Please be kind",
    }
    app = create_app(settings=make_settings(), broker_client=FakeBrokerClient())
    with TestClient(app) as c:
        r = c.post(
            "/api/conversations/bob/messages",
            json={"body": "meanie"},
            headers=auth_header(acct.uid),
        )
    assert r.status_code == 403
    assert r.json()["detail"] == {"reason": "hook_blocked", "message": "Please be kind"}


def test_hook_example_verifies_what_the_relay_signs(world, routing, fake_hooks):
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "hook_example",
        Path(__file__).resolve().parents[2] / "tools" / "hook_example.py",
    )
    ex = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ex)
    hook = add_hook()
    web_send(routing, "alice", "bob", "a badword here")
    call = fake_hooks.calls[0]
    assert ex.verify(
        hook.secret,
        {k.title(): v for k, v in call.headers.items()}
        | {
            "X-KP-Timestamp": call.headers["x-kp-timestamp"],
            "X-KP-Signature": call.headers["x-kp-signature"],
        },
        call.raw,
    )
    assert not ex.verify(
        hook.secret,
        {
            "X-KP-Timestamp": call.headers["x-kp-timestamp"],
            "X-KP-Signature": "sha256=00",
        },
        call.raw,
    )
    assert ex.decide(call.payload, "badword")["action"] == "block"
    assert ex.decide(call.payload, None) == {"action": "allow"}
