"""Processor queue and the `/hooks/v1` callback API."""

# ruff: noqa: F811 -- fixtures imported from tests/hooks_world.py
from __future__ import annotations

from collections.abc import Iterator

import httpx
import pytest
from fastapi.testclient import TestClient

from app import hooks as hooks_module
from app.main import create_app
from app.routing import Routing
from app.store import alerts as alerts_store
from app.store import hooks as hooks_store
from app.store import messages as messages_store
from tests.fake_transport import FakeBrokerClient
from tests.hooks_world import (
    FakeHooks,
    add_hook,
    befriend,
    fake_hooks,  # noqa: F401 -- fixture
    make_user,
    routing,  # noqa: F401 -- fixture
    web_send,
)
from tests.test_admin import make_settings


@pytest.fixture
def world():
    make_user("alice", "alice", "fam1", role="admin")
    make_user("bob", "bob", "fam1")
    make_user("carol", "carol", "fam2")
    befriend("alice", "bob")
    befriend("alice", "carol")


@pytest.fixture
def waits() -> Iterator[list[float]]:
    """Run processor deliveries inline and record the waits instead of sleeping."""
    recorded: list[float] = []
    hooks_module.set_background(spawn=lambda fn: fn(), sleep=recorded.append)
    yield recorded
    hooks_module.set_background()


@pytest.fixture
def client() -> Iterator[TestClient]:
    app = create_app(settings=make_settings(), broker_client=FakeBrokerClient())
    with TestClient(app) as c:
        yield c


def test_processor_gets_ids_and_does_not_delay_or_change_the_message(
    world, routing: Routing, fake_hooks: FakeHooks, waits
):
    h = add_hook(mode="processor")
    fake_hooks.respond["h1.test"] = {"action": "block"}  # response body is ignored
    r = web_send(routing, "alice", "bob", "hello", wire_id="w9")
    assert [m.body for m in r.messages] == ["hello"]
    [call] = fake_hooks.calls
    p = call.payload
    assert p["mode"] == "processor" and p["message"]["ids"] == [r.messages[0].id]
    assert p["message"]["ref"] == "w9" and p["message"]["body"] == "hello"
    assert call.headers["x-kp-hook"] == h.id
    assert waits == []
    assert hooks_store.get(h.id).stats.calls == 1


def test_processor_is_not_called_for_a_blocked_or_duplicate_message(
    world, routing, fake_hooks, waits
):
    add_hook("f.test", order=1)
    add_hook("p.test", mode="processor")
    fake_hooks.respond["f.test"] = {"action": "block"}
    web_send(routing, "alice", "bob", "x")
    assert fake_hooks.to("p.test") == []
    fake_hooks.respond["f.test"] = {"action": "allow"}
    web_send(routing, "alice", "bob", "y", wire_id="u1")
    web_send(routing, "alice", "bob", "y", wire_id="u1")
    assert len(fake_hooks.to("p.test")) == 1


def test_processor_sees_the_final_body_after_a_filter_rewrote_it(
    world, routing, fake_hooks, waits
):
    add_hook("f.test", order=1)
    add_hook("p.test", mode="processor")
    fake_hooks.respond["f.test"] = {"action": "replace", "body": "polite"}
    web_send(routing, "alice", "bob", "rude")
    assert fake_hooks.to("p.test")[0].payload["message"]["body"] == "polite"


def test_retries_at_30s_and_5min_then_drops_and_counts(
    world, routing, fake_hooks, waits
):
    h = add_hook(mode="processor")
    fake_hooks.respond["h1.test"] = lambda c: httpx.Response(500)
    web_send(routing, "alice", "bob", "x")
    assert waits == [30.0, 300.0]
    assert len(fake_hooks.calls) == 3
    st = hooks_store.get(h.id).stats
    assert st.errors == 3 and st.dropped == 1 and st.calls == 3
    # the same event id on every attempt, so a processor can dedupe
    assert len({c.payload["id"] for c in fake_hooks.calls}) == 1


def test_retry_stops_on_success(world, routing, fake_hooks, waits):
    add_hook(mode="processor")
    answers = iter([httpx.ReadTimeout("t"), httpx.Response(204)])

    def respond(call):
        a = next(answers)
        if isinstance(a, Exception):
            raise a
        return a

    fake_hooks.respond["h1.test"] = respond
    web_send(routing, "alice", "bob", "x")
    assert waits == [30.0] and len(fake_hooks.calls) == 2
    assert hooks_store.list_hooks()[0].stats.dropped == 0


def test_a_hook_disabled_while_waiting_gets_no_retry(world, routing, fake_hooks):
    h = add_hook(mode="processor")
    fake_hooks.respond["h1.test"] = lambda c: httpx.Response(500)

    def disable_then_continue(_delay: float) -> None:
        hooks_store.update(h.id, {"enabled": False})

    hooks_module.set_background(spawn=lambda fn: fn(), sleep=disable_then_continue)
    try:
        web_send(routing, "alice", "bob", "x")
    finally:
        hooks_module.set_background()
    assert len(fake_hooks.calls) == 1


def test_default_runner_is_a_background_thread(world, routing, fake_hooks):
    import threading

    done = threading.Event()
    fake_hooks.respond["h1.test"] = lambda c: (done.set(), httpx.Response(200))[1]
    add_hook(mode="processor")
    web_send(routing, "alice", "bob", "x")
    assert done.wait(5)


def _auth(hook) -> dict[str, str]:
    return {"Authorization": f"Bearer {hook.secret}"}


def test_bearer_auth(world, client: TestClient):
    h = add_hook(mode="processor")
    url = f"/hooks/v1/{h.id}/ping"
    assert client.get(url).status_code == 401
    assert client.get(url, headers={"Authorization": "Bearer nope"}).status_code == 401
    assert client.get("/hooks/v1/hk_missing/ping", headers=_auth(h)).status_code == 401
    ok = client.get(url, headers=_auth(h))
    assert ok.status_code == 200 and ok.json()["hook"] == h.id
    other = add_hook("o.test", mode="processor")
    assert client.get(url, headers=_auth(other)).status_code == 401
    hooks_store.update(h.id, {"enabled": False})
    assert client.get(url, headers=_auth(h)).status_code == 403


def test_rate_limit_120_per_minute(world, client: TestClient):
    h = add_hook(mode="processor")
    codes = [
        client.get(f"/hooks/v1/{h.id}/ping", headers=_auth(h)).status_code
        for _ in range(121)
    ]
    assert codes[:120] == [200] * 120 and codes[120] == 429


def test_action_applies_alert_and_annotation(
    world, routing, fake_hooks, waits, client: TestClient
):
    h = add_hook(mode="processor")
    r = web_send(routing, "alice", "carol", "mean words", wire_id="w5")
    mid = r.messages[0].id
    res = client.post(
        f"/hooks/v1/{h.id}/actions",
        headers=_auth(h),
        json={
            "ref": "w5",
            "labels": ["bullying", "x" * 40],
            "reason": "pattern",
            "alert": {"title": "Possible bullying", "body": "three insults in a row"},
        },
    )
    assert res.status_code == 200 and res.json() == {"updated": 1, "alerts": 2}
    ann = messages_store.get_message(mid).hooks[h.id]
    assert ann["labels"] == ["bullying", "x" * 24] and ann["reason"] == "pattern"
    assert ann["action"] == "annotate"
    for fam in ("fam1", "fam2"):
        [a] = alerts_store.list_alerts(fam, "all")
        assert (
            a.kind == "hook_flag"
            and a.title == "Possible bullying"
            and a.hookId == h.id
        )
    # by message id; labels merge, the alert is deduplicated for the hour
    res = client.post(
        f"/hooks/v1/{h.id}/actions",
        headers=_auth(h),
        json={
            "messageId": mid,
            "labels": ["again"],
            "alert": {"title": "t", "body": "b"},
        },
    )
    assert res.json() == {"updated": 1, "alerts": 0}
    assert messages_store.get_message(mid).hooks[h.id]["labels"] == [
        "bullying",
        "x" * 24,
        "again",
    ]


def test_action_validation_and_scope(
    world, routing, fake_hooks, waits, client: TestClient
):
    mine = add_hook("m.test", mode="processor", families=["fam1", "fam2"])
    outsider = add_hook("o.test", mode="processor", families=["fam9"])
    only2 = add_hook("p.test", mode="processor", families=["fam2"])
    r = web_send(routing, "alice", "bob", "x", wire_id="w1")  # fam1 only
    web_send(routing, "alice", "carol", "y", wire_id="w2")  # fam1 + fam2
    # a hook must cover every family of the message
    for h, ref in ((only2, "w1"), (only2, "w2")):
        assert (
            client.post(
                f"/hooks/v1/{h.id}/actions",
                headers=_auth(h),
                json={"ref": ref, "labels": ["a"]},
            ).status_code
            == 404
        )
    assert (
        client.post(
            f"/hooks/v1/{outsider.id}/actions",
            headers=_auth(outsider),
            json={"ref": "w2", "labels": ["a"]},
        ).status_code
        == 404
    )
    assert (
        client.post(
            f"/hooks/v1/{mine.id}/actions",
            headers=_auth(mine),
            json={"ref": "w2", "labels": ["a"]},
        ).status_code
        == 200
    )
    assert (
        client.post(
            f"/hooks/v1/{mine.id}/actions", headers=_auth(mine), json={}
        ).status_code
        == 422
    )
    assert (
        client.post(
            f"/hooks/v1/{mine.id}/actions",
            headers=_auth(mine),
            json={"ref": "w2", "messageId": r.messages[0].id},
        ).status_code
        == 422
    )
    assert (
        client.post(
            f"/hooks/v1/{mine.id}/actions",
            headers=_auth(mine),
            json={"ref": "nope", "labels": ["a"]},
        ).status_code
        == 404
    )


def test_history_endpoint_scope_and_paging(
    world, routing, fake_hooks, waits, client: TestClient
):
    h = add_hook("m.test", mode="processor", families=["fam1"])
    for i in range(6):
        web_send(routing, "alice", "bob", f"m{i}")
    web_send(routing, "alice", "carol", "cross")
    base = f"/hooks/v1/{h.id}/conversations"
    got = client.get(f"{base}/alice_bob/messages?limit=4", headers=_auth(h)).json()[
        "messages"
    ]
    assert [m["body"] for m in got] == ["m2", "m3", "m4", "m5"]
    assert set(got[0]) == {"id", "ts", "sender", "senderUid", "body"}
    older = client.get(
        f"{base}/alice_bob/messages?limit=10&before={got[0]['id']}", headers=_auth(h)
    ).json()["messages"]
    assert [m["body"] for m in older] == ["m0", "m1"]
    assert (
        client.get(f"{base}/alice_bob/messages?limit=201", headers=_auth(h)).status_code
        == 422
    )
    assert client.get(f"{base}/nope/messages", headers=_auth(h)).status_code == 404

    only2 = add_hook("o.test", mode="processor", families=["fam2"])
    both = add_hook("b.test", mode="processor", families=["fam1", "fam2"])
    # alice_bob is fam1 only: refused for a fam2-only hook; alice_carol spans both and is
    # refused to a hook that covers just one of them
    assert (
        client.get(
            f"/hooks/v1/{only2.id}/conversations/alice_bob/messages",
            headers=_auth(only2),
        ).status_code
        == 404
    )
    assert (
        client.get(
            f"/hooks/v1/{only2.id}/conversations/alice_carol/messages",
            headers=_auth(only2),
        ).status_code
        == 404
    )
    assert (
        client.get(
            f"/hooks/v1/{h.id}/conversations/alice_carol/messages", headers=_auth(h)
        ).status_code
        == 404
    )
    assert (
        client.get(
            f"/hooks/v1/{both.id}/conversations/alice_carol/messages",
            headers=_auth(both),
        ).status_code
        == 200
    )
