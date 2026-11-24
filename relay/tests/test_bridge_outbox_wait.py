"""`outboxWaitS` hint and the change-feed outbox wait."""

from __future__ import annotations

import dataclasses
import os
import threading
import time

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.store import bridge_outbox
from tests.bridge_world import (  # noqa: F401
    MOM,
    RecordingFCM,
    World,
    broker,
    client,
    fcm,
    make_settings,
    pair_bridge,
    world,
)
from tests.fake_transport import FakeBrokerClient

SQLITE = (os.environ.get("DB_BACKEND", "").strip().lower() or "firestore") == "sqlite"
needs_sqlite = pytest.mark.skipif(not SQLITE, reason="the change feed exists only under DB_BACKEND=sqlite (§3.3)")


@pytest.fixture
def hinted_client(broker: FakeBrokerClient, fcm: RecordingFCM):
    settings = dataclasses.replace(make_settings(), bridge_outbox_wait_s=7)
    with TestClient(create_app(settings=settings, broker_client=broker)) as c:
        yield c


@pytest.mark.parametrize(
    ("fixture", "hint"),
    [("client", None), ("hinted_client", 7)],
    ids=["default-no-hint", "hint-set"],
)
def test_outbox_wait_hint_in_pair_and_heartbeat_only_when_set(
    request: pytest.FixtureRequest, world: World, fixture: str, hint: int | None
):
    from app.store import bridges as bridges_store

    c = request.getfixturevalue(fixture)
    bridge = bridges_store.create("kid", world.family_id, "phone", "adm")
    code, _ = bridges_store.create_pair_code(bridge.id)
    r = c.post("/bridge/pair", json={"code": code, "version": "1.0", "caps": {"sms": True}})
    assert r.status_code == 200
    expected = {"pending": 0}
    if hint:
        assert r.json()["outboxWaitS"] == hint
        expected["outboxWaitS"] = hint
    else:
        assert set(r.json()) == {"bridgeId", "token"}
    headers = {"Authorization": f"Bearer {r.json()['token']}"}
    hb = c.post("/bridge/heartbeat", json={"status": {}}, headers=headers)
    assert hb.json() == expected


def test_settings_clamp(monkeypatch):
    from app.config import _bridge_outbox_wait_s as f

    assert (f(""), f("0"), f("12"), f("99"), f("-3")) == (0, 0, 12, 25, 0)


def _enqueue_later(bridge, delay: float) -> threading.Thread:
    def run():
        time.sleep(delay)
        bridge_outbox.enqueue_send(bridge, "m_w", "sms", source="sms", to={"phone": MOM}, text="x")

    t = threading.Thread(target=run)
    t.start()
    return t


@needs_sqlite
def test_enqueue_wakes_a_waiting_poll_at_once(client: TestClient, world: World):
    bridge, headers = pair_bridge(client, world)
    t = _enqueue_later(bridge, 0.3)
    start = time.monotonic()
    r = client.get("/bridge/outbox?wait=5", headers=headers)
    elapsed = time.monotonic() - start
    t.join()
    assert [i["id"] for i in r.json()["items"]] == ["ob_m_w_sms"]
    assert elapsed < 1.5, elapsed


@needs_sqlite
def test_other_bridges_writes_do_not_end_the_wait_early(client: TestClient, world: World):
    bridge, headers = pair_bridge(client, world)
    start = time.monotonic()
    assert client.get("/bridge/outbox?wait=1", headers=headers).json() == {"items": []}
    assert 0.9 <= time.monotonic() - start < 2.5


@pytest.mark.skipif(SQLITE, reason="the 2 s poll is the Firestore path")
def test_firestore_path_still_polls_every_two_seconds(client: TestClient, world: World):
    bridge, headers = pair_bridge(client, world)
    t = _enqueue_later(bridge, 0.3)
    start = time.monotonic()
    r = client.get("/bridge/outbox?wait=5", headers=headers)
    elapsed = time.monotonic() - start
    t.join()
    assert len(r.json()["items"]) == 1
    assert 1.5 <= elapsed < 3.5, elapsed
