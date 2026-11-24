"""`/api/live` SSE stream tests. Read with a minimal ASGI driver: httpx's and Starlette's test
transports buffer a response until it ends."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from firebase_admin import auth as fb_auth

from app import live
from app.auth import set_claims
from app.db.firestore import get_db
from app.main import create_app
from app.store import messages as messages_store
from app.store import users as users_store
from tests.fake_transport import FakeBrokerClient
from tests.firebase_test_utils import auth_header
from tests.test_auth import make_settings

SQLITE = (os.environ.get("DB_BACKEND", "").strip().lower() or "firestore") == "sqlite"
needs_sqlite = pytest.mark.skipif(
    not SQLITE, reason="the change feed exists only under DB_BACKEND=sqlite (§3.3)"
)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def test_router_registered_only_under_sqlite():
    from fastapi.testclient import TestClient

    for backend, status in (("firestore", 404), ("sqlite", 401)):
        app = create_app(settings=make_settings(db_backend=backend), broker_client=FakeBrokerClient())
        with TestClient(app) as client:
            assert client.get("/api/live?stream=s1").status_code == status, backend
            assert client.post("/api/live/s1/subscribe", json={}).status_code == status, backend


class Stream:
    """One open `GET /api/live` against the ASGI app."""

    def __init__(self, app: Any, stream_id: str, headers: dict[str, str]) -> None:
        self.app = app
        self.stream_id = stream_id
        self.headers = headers
        self.status: int | None = None
        self.events: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._buf = b""
        self._started = asyncio.Event()
        self._gone = asyncio.Event()
        self._done = asyncio.Event()

    async def _receive(self) -> dict[str, Any]:
        await self._gone.wait()
        return {"type": "http.disconnect"}

    async def _send(self, msg: dict[str, Any]) -> None:
        if msg["type"] == "http.response.start":
            self.status = msg["status"]
            self._started.set()
        elif msg["type"] == "http.response.body":
            self._buf += msg.get("body", b"")
            while b"\n\n" in self._buf:
                block, self._buf = self._buf.split(b"\n\n", 1)
                for line in block.decode().split("\n"):
                    if line.startswith("data: "):
                        self.events.put_nowait(json.loads(line[6:]))
                    elif line.startswith(":"):
                        self.events.put_nowait({"comment": line[1:].strip()})
            if not msg.get("more_body", False):
                self._done.set()

    async def open(self) -> None:
        headers = [(b"host", b"test")] + [
            (k.lower().encode(), v.encode()) for k, v in self.headers.items()
        ]
        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/api/live",
            "raw_path": b"/api/live",
            "query_string": f"stream={self.stream_id}".encode(),
            "root_path": "",
            "headers": headers,
            "client": ("test", 1),
            "server": ("test", 80),
            "state": {},
        }
        self.task = asyncio.create_task(self.app(scope, self._receive, self._send))
        await asyncio.wait_for(self._started.wait(), 5)

    async def next(self, timeout: float = 2.0) -> dict[str, Any]:
        while True:
            ev = await asyncio.wait_for(self.events.get(), timeout)
            if "comment" not in ev:
                return ev

    async def none_within(self, seconds: float = 0.2) -> None:
        try:
            ev = await self.next(seconds)
        except TimeoutError:
            return
        raise AssertionError(f"unexpected event {ev}")

    async def close(self) -> None:
        self._gone.set()
        await asyncio.wait_for(self.task, 5)


@pytest.fixture
async def world() -> AsyncIterator[dict[str, Any]]:
    app = create_app(settings=make_settings(db_backend="sqlite"), broker_client=FakeBrokerClient())
    for uid, alias, role, fam in (
        ("u1", "alice", "member", "famA"),
        ("u2", "bob", "member", "famA"),
        ("u3", "carol", "member", "famB"),
    ):
        fb_auth.create_user(uid=uid, email=f"{uid}@example.com")
        users_store.create_user(uid=uid, alias=alias, display_name=alias, role=role, family_id=fam)
        set_claims(uid, role, fam)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as http:
        yield {"app": app, "http": http, "hdr": {u: auth_header(u) for u in ("u1", "u2", "u3")}}


async def _open(world: dict[str, Any], uid: str, stream_id: str) -> Stream:
    st = Stream(world["app"], stream_id, world["hdr"][uid])
    await st.open()
    assert st.status == 200
    return st


async def _sub(world: dict[str, Any], uid: str, stream_id: str, sub: str, query: dict) -> httpx.Response:
    return await world["http"].post(
        f"/api/live/{stream_id}/subscribe",
        json={"sub": sub, "query": query},
        headers=world["hdr"][uid],
    )


def _thread(uid: str, key: str) -> dict:
    return {
        "collection": "messages",
        "where": [["convKey", "==", key], ["uids", "array-contains", uid]],
        "orderBy": [["seq", "desc"]],
        "limit": 50,
    }


@needs_sqlite
@pytest.mark.anyio
async def test_snapshot_then_added_modified_removed(world):
    first = messages_store.create_message(sender_uid="u1", recipient_uid="u2", kind="text", ts=1, body="a")
    key = messages_store.conv_key("u1", "u2")
    st = await _open(world, "u1", "s1")
    try:
        resp = await _sub(world, "u1", "s1", "t", _thread("u1", key))
        assert resp.status_code == 200, resp.text
        snap = await st.next()
        assert snap["sub"] == "t" and snap["type"] == "snapshot"
        assert [d["path"] for d in snap["docs"]] == [f"messages/{first.id}"]
        assert snap["docs"][0]["data"]["body"] == "a"

        second = messages_store.create_message(
            sender_uid="u2", recipient_uid="u1", kind="text", ts=2, body="b"
        )
        ev = await st.next()
        assert (ev["type"], ev["id"], ev["data"]["body"]) == ("added", second.id, "b")

        get_db().collection("messages").document(second.id).update({"body": "b2"})
        ev = await st.next()
        assert (ev["type"], ev["id"], ev["data"]["body"]) == ("modified", second.id, "b2")

        # Out of scope: another pair's message, and a write elsewhere.
        messages_store.create_message(sender_uid="u2", recipient_uid="u3", kind="text", ts=3, body="x")
        get_db().collection("settings").document("noise").set({"n": 1})
        await st.none_within()

        get_db().collection("messages").document(second.id).delete()
        ev = await st.next()
        assert (ev["type"], ev["id"], ev["data"]) == ("removed", second.id, None)
    finally:
        await st.close()


@needs_sqlite
@pytest.mark.anyio
async def test_document_that_stops_matching_is_removed(world):
    st = await _open(world, "u1", "s1")
    try:
        q = {"collection": "users", "where": [["familyId", "==", "famA"]]}
        assert (await _sub(world, "u1", "s1", "fam", q)).status_code == 200
        snap = await st.next()
        assert sorted(d["id"] for d in snap["docs"]) == ["u1", "u2"]
        get_db().collection("users").document("u2").update({"familyId": "famB"})
        ev = await st.next()
        assert (ev["type"], ev["id"]) == ("removed", "u2")
        get_db().collection("users").document("u3").update({"familyId": "famA"})
        ev = await st.next()
        assert (ev["type"], ev["id"]) == ("added", "u3")
    finally:
        await st.close()


@needs_sqlite
@pytest.mark.anyio
async def test_can_read_is_rechecked_on_every_event(world):
    st = await _open(world, "u1", "s1")
    try:
        assert (await _sub(world, "u1", "s1", "peer", {"doc": "users/u2"})).status_code == 200
        snap = await st.next()
        assert [d["id"] for d in snap["docs"]] == ["u2"]
        get_db().collection("users").document("u2").update({"familyId": "famB"})
        ev = await st.next()
        assert (ev["type"], ev["id"]) == ("removed", "u2")
        get_db().collection("users").document("u2").update({"displayName": "secret"})
        await st.none_within()
    finally:
        await st.close()


@needs_sqlite
@pytest.mark.anyio
async def test_snapshot_drops_documents_can_read_refuses(world):
    get_db().collection("conversations").document("c1").set({"uids": ["u1", "u2"], "familyIds": ["famA"]})
    get_db().collection("conversations").document("c2").set({"uids": ["u3", "u9"], "familyIds": ["famB"]})
    get_db().collection("users").document("u1").update({"role": "admin"})
    set_claims("u1", "admin", "famA")
    world["hdr"]["u1"] = auth_header("u1")
    st = await _open(world, "u1", "s1")
    try:
        q = {"collection": "conversations", "where": [["familyIds", "array-contains", "famA"]]}
        assert (await _sub(world, "u1", "s1", "mon", q)).status_code == 200
        snap = await st.next()
        assert [d["id"] for d in snap["docs"]] == ["c1"]
        get_db().collection("conversations").document("c2").update({"familyIds": ["famA", "famB"]})
        ev = await st.next()
        assert (ev["type"], ev["id"]) == ("added", "c2")
    finally:
        await st.close()


@needs_sqlite
@pytest.mark.anyio
@pytest.mark.parametrize(
    ("query", "status"),
    [
        pytest.param(
            {"collection": "messages", "where": [["convKey", "==", messages_store.conv_key("u1", "u2")]]},
            403,
            id="messages-by-convKey",
        ),
        pytest.param({"collection": "families"}, 403, id="families"),
        pytest.param({"collection": "deviceSecrets"}, 403, id="deviceSecrets"),
        pytest.param({"collection": "users", "where": [["familyId", ">=", "famA"]]}, 400, id="bad-operator"),
        pytest.param({"collection": "users/u1"}, 400, id="collection-path"),
        pytest.param({"doc": "users"}, 400, id="doc-not-a-doc-path"),
        pytest.param({"collection": "users", "limit": 0}, 400, id="limit-zero"),
    ],
)
async def test_bad_subscribe_query_is_refused_with_its_status_and_no_event(world, query, status):
    st = await _open(world, "u1", "s1")
    try:
        assert (await _sub(world, "u1", "s1", "q", query)).status_code == status
        await st.none_within()
    finally:
        await st.close()


@needs_sqlite
@pytest.mark.anyio
async def test_unknown_or_someone_elses_stream_is_404(world):
    assert (await _sub(world, "u1", "nope", "t", {"doc": "users/u1"})).status_code == 404
    st = await _open(world, "u1", "s1")
    try:
        assert (await _sub(world, "u2", "s1", "t", {"doc": "users/u2"})).status_code == 404
        resp = await world["http"].delete("/api/live/s1/subscribe/t", headers=world["hdr"]["u2"])
        assert resp.status_code == 404
    finally:
        await st.close()
    # Closed with the connection.
    assert "s1" not in live._streams
    assert (await _sub(world, "u1", "s1", "t", {"doc": "users/u1"})).status_code == 404


@needs_sqlite
@pytest.mark.anyio
async def test_unsubscribe_stops_events_and_close_drops_the_feed_listener(world):
    listeners_before = len(get_db()._listeners)
    st = await _open(world, "u1", "s1")
    assert len(get_db()._listeners) == listeners_before + 1
    try:
        assert (await _sub(world, "u1", "s1", "me", {"doc": "users/u1"})).status_code == 200
        await st.next()
        resp = await world["http"].delete("/api/live/s1/subscribe/me", headers=world["hdr"]["u1"])
        assert resp.status_code == 204
        get_db().collection("users").document("u1").update({"displayName": "x"})
        await st.none_within()
    finally:
        await st.close()
    assert len(get_db()._listeners) == listeners_before


@needs_sqlite
@pytest.mark.anyio
async def test_snapshot_honours_order_and_limit_and_encodes_timestamps(world):
    key = messages_store.conv_key("u1", "u2")
    for i in range(3):
        messages_store.create_message(sender_uid="u1", recipient_uid="u2", kind="text", ts=i, body=str(i))
    at = datetime(2026, 10, 31, 12, 0, 0, 123456, tzinfo=UTC)
    get_db().collection("settings").document("stamp").set({"at": at, "raw": b"\x00\x01"})
    st = await _open(world, "u1", "s1")
    try:
        q = _thread("u1", key) | {"limit": 2}
        assert (await _sub(world, "u1", "s1", "t", q)).status_code == 200
        snap = await st.next()
        seqs = [d["data"]["seq"] for d in snap["docs"]]
        assert len(seqs) == 2 and seqs == sorted(seqs, reverse=True)

        assert (await _sub(world, "u1", "s1", "s", {"doc": "settings/stamp"})).status_code == 200
        snap = await st.next()
        data = snap["docs"][0]["data"]
        assert data["at"] == {"$ts": int(at.timestamp()) * 1_000_000 + 123456}
        assert data["raw"] == {"$b64": "AAE="}
    finally:
        await st.close()


@needs_sqlite
@pytest.mark.anyio
async def test_heartbeat_comment(world, monkeypatch):
    monkeypatch.setattr(live, "HEARTBEAT_S", 0.05)
    st = await _open(world, "u1", "s1")
    try:
        ev = await asyncio.wait_for(st.events.get(), 2)
        while "comment" in ev and ev["comment"] != "hb":
            ev = await asyncio.wait_for(st.events.get(), 2)
        assert ev == {"comment": "hb"}
    finally:
        await st.close()


@needs_sqlite
@pytest.mark.anyio
async def test_resubscribe_after_reconnect_gets_a_fresh_snapshot(world):
    st = await _open(world, "u1", "s1")
    assert (await _sub(world, "u1", "s1", "me", {"doc": "users/u1"})).status_code == 200
    await st.next()
    await st.close()
    get_db().collection("users").document("u1").update({"displayName": "while away"})
    st = await _open(world, "u1", "s1")
    try:
        assert (await _sub(world, "u1", "s1", "me", {"doc": "users/u1"})).status_code == 200
        snap = await st.next()
        assert snap["docs"][0]["data"]["displayName"] == "while away"
    finally:
        await st.close()


@needs_sqlite
@pytest.mark.anyio
async def test_parent_device_change_reevaluates_its_locations(world):
    db = get_db()
    db.collection("devices").document("pgr-1").set(
        {"familyId": "famA", "ownerUid": "u2", "locatableBy": ["u1"]}
    )
    db.collection("devices").document("pgr-1").collection("locations").document("l1").set(
        {"lat": 1.0, "lon": 2.0, "ts": 1}
    )
    st = await _open(world, "u1", "s1")
    try:
        q = {"collection": "devices/pgr-1/locations", "orderBy": [["ts", "desc"]], "limit": 5}
        assert (await _sub(world, "u1", "s1", "loc", q)).status_code == 200
        snap = await st.next()
        assert [d["id"] for d in snap["docs"]] == ["l1"]

        db.collection("devices").document("pgr-1").update({"locatableBy": []})
        ev = await st.next()
        assert (ev["sub"], ev["type"], ev["docs"]) == ("loc", "snapshot", [])
        # And a later location is not sent.
        db.collection("devices").document("pgr-1").collection("locations").document("l2").set(
            {"lat": 3.0, "lon": 4.0, "ts": 2}
        )
        await st.none_within()

        db.collection("devices").document("pgr-1").update({"locatableBy": ["u1"]})
        ev = await st.next()
        assert ev["type"] == "snapshot" and sorted(d["id"] for d in ev["docs"]) == ["l1", "l2"]
        # Another device's change does not touch this subscription.
        db.collection("devices").document("pgr-2").set({"familyId": "famA", "ownerUid": "u1"})
        await st.none_within()
    finally:
        await st.close()


@needs_sqlite
@pytest.mark.anyio
async def test_removal_under_a_limit_backfills_the_next_document(world):
    key = messages_store.conv_key("u1", "u2")
    made = [
        messages_store.create_message(sender_uid="u1", recipient_uid="u2", kind="text", ts=i, body=str(i))
        for i in range(3)
    ]
    st = await _open(world, "u1", "s1")
    try:
        assert (await _sub(world, "u1", "s1", "t", _thread("u1", key) | {"limit": 2})).status_code == 200
        snap = await st.next()
        assert sorted(d["id"] for d in snap["docs"]) == sorted(m.id for m in made[1:])

        get_db().collection("messages").document(made[2].id).delete()
        ev = await st.next()
        assert ev["type"] == "snapshot"
        assert sorted(d["id"] for d in ev["docs"]) == sorted(m.id for m in made[:2])
    finally:
        await st.close()
