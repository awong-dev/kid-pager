"""`/api/dev/db/*`: DEV_MODE-only raw document routes."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from app.db.firestore import get_db
from app.main import create_app
from tests.fake_transport import FakeBrokerClient
from tests.test_healthz import make_settings

BASE = "/api/dev/db"
TS_US = 1_700_000_000_123_456


@pytest.fixture
def client() -> Iterator[TestClient]:
    app = create_app(
        settings=make_settings(dev_mode=True), broker_client=FakeBrokerClient()
    )
    with TestClient(app) as c:
        yield c


def put(client: TestClient, path: str, data: dict) -> None:
    assert (
        client.put(f"{BASE}/doc", json={"path": path, "data": data}).status_code == 200
    )


def test_routes_404_without_dev_mode():
    app = create_app(
        settings=make_settings(dev_mode=False), broker_client=FakeBrokerClient()
    )
    with TestClient(app) as c:
        assert c.get(f"{BASE}/doc", params={"path": "a/b"}).status_code == 404
        assert c.put(f"{BASE}/doc", json={"path": "a/b", "data": {}}).status_code == 404
        assert (
            c.patch(f"{BASE}/doc", json={"path": "a/b", "data": {}}).status_code == 404
        )
        assert c.delete(f"{BASE}/doc", params={"path": "a/b"}).status_code == 404
        assert c.get(f"{BASE}/list", params={"group": "b"}).status_code == 404
        assert c.post(f"{BASE}/wipe").status_code == 404


def test_put_decodes_wire_values_and_get_encodes_them(client: TestClient):
    put(client, "devdbt/d1", {"at": {"$ts": TS_US}, "blob": {"$b64": "AQID"}, "n": 3})
    stored = get_db().collection("devdbt").document("d1").get().to_dict()
    assert isinstance(stored["at"], datetime) and stored["at"].tzinfo is not None
    assert stored["at"] == datetime.fromtimestamp(TS_US / 1_000_000, tz=UTC)
    assert bytes(stored["blob"]) == b"\x01\x02\x03"
    body = client.get(f"{BASE}/doc", params={"path": "devdbt/d1"}).json()
    assert body["exists"] is True
    assert body["data"] == {"at": {"$ts": TS_US}, "blob": {"$b64": "AQID"}, "n": 3}


def test_get_missing_doc(client: TestClient):
    body = client.get(f"{BASE}/doc", params={"path": "devdbt/nope"}).json()
    assert body["exists"] is False and body["data"] is None


def test_patch_merges(client: TestClient):
    put(client, "devdbt/d1", {"a": 1, "b": 2})
    assert client.patch(
        f"{BASE}/doc", json={"path": "devdbt/d1", "data": {"b": 5, "c": 6}}
    ).json() == {"ok": True}
    data = client.get(f"{BASE}/doc", params={"path": "devdbt/d1"}).json()["data"]
    assert data == {"a": 1, "b": 5, "c": 6}


def test_put_replaces(client: TestClient):
    put(client, "devdbt/d1", {"a": 1})
    put(client, "devdbt/d1", {"b": 2})
    assert client.get(f"{BASE}/doc", params={"path": "devdbt/d1"}).json()["data"] == {
        "b": 2
    }


def test_delete_removes(client: TestClient):
    put(client, "devdbt/d1", {"a": 1})
    assert client.delete(f"{BASE}/doc", params={"path": "devdbt/d1"}).json() == {
        "ok": True
    }
    assert (
        client.get(f"{BASE}/doc", params={"path": "devdbt/d1"}).json()["exists"]
        is False
    )


def test_list_by_parent(client: TestClient):
    put(client, "devices/d1/locations/l1", {"v": 1})
    put(client, "devices/d1/locations/l2", {"v": 2})
    put(client, "devices/d2/locations/l3", {"v": 3})
    put(client, "devices/d1", {"name": "x"})
    docs = client.get(f"{BASE}/list", params={"parent": "devices/d1/locations"}).json()[
        "docs"
    ]
    assert sorted(d["path"] for d in docs) == [
        "devices/d1/locations/l1",
        "devices/d1/locations/l2",
    ]
    by_id = {d["id"]: d["data"] for d in docs}
    assert by_id == {"l1": {"v": 1}, "l2": {"v": 2}}


def test_list_by_group_spans_parents(client: TestClient):
    put(client, "devices/d1/locations/l1", {"v": 1})
    put(client, "devices/d2/locations/l3", {"v": 3})
    docs = client.get(f"{BASE}/list", params={"group": "locations"}).json()["docs"]
    assert sorted(d["path"] for d in docs) == [
        "devices/d1/locations/l1",
        "devices/d2/locations/l3",
    ]


def test_bad_paths_are_400(client: TestClient):
    assert client.get(f"{BASE}/list").status_code == 400
    both = {"parent": "devices", "group": "devices"}
    assert client.get(f"{BASE}/list", params=both).status_code == 400
    assert (
        client.get(f"{BASE}/list", params={"parent": "devices/d1"}).status_code == 400
    )
    assert client.get(f"{BASE}/doc", params={"path": "devices"}).status_code == 400
    assert (
        client.put(f"{BASE}/doc", json={"path": "devices", "data": {}}).status_code
        == 400
    )
    assert client.delete(f"{BASE}/doc", params={"path": "devices"}).status_code == 400


def test_wipe_removes_everything(client: TestClient):
    put(client, "devdbt/d1", {"a": 1})
    put(client, "devdbt/d2", {"a": 2})
    put(client, "devices/d1/locations/l1", {"v": 1})
    deleted = client.post(f"{BASE}/wipe").json()["deleted"]
    assert deleted >= 3
    assert (
        client.get(f"{BASE}/doc", params={"path": "devdbt/d1"}).json()["exists"]
        is False
    )
    assert (
        client.get(f"{BASE}/list", params={"group": "locations"}).json()["docs"] == []
    )
