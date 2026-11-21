"""`/api/admin/hooks` and the family disclosure."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from firebase_admin import auth as fb_auth

from app.main import create_app
from app.store import families as families_store
from app.store import hooks as hooks_store
from app.store import users as users_store
from tests.fake_transport import FakeBrokerClient
from tests.firebase_test_utils import auth_header
from tests.test_admin import make_settings


@pytest.fixture
def client() -> Iterator[TestClient]:
    app = create_app(settings=make_settings(), broker_client=FakeBrokerClient())
    with TestClient(app) as c:
        yield c


def _user(email: str, alias: str, role: str, family: str) -> dict[str, str]:
    acct = fb_auth.create_user(email=email)
    users_store.create_user(
        uid=acct.uid, alias=alias, display_name=alias, role=role, family_id=family
    )
    fb_auth.set_custom_user_claims(acct.uid, {"role": role, "fam": family})
    return auth_header(acct.uid)


@pytest.fixture
def root() -> dict[str, str]:
    return _user("root@example.com", "root", "super", "default")


BODY = {"name": "guard", "url": "http://hook.local:9000/h", "mode": "filter"}


def test_create_shows_secret_once_and_list_omits_it(client: TestClient, root):
    r = client.post("/api/admin/hooks", json=BODY, headers=root)
    assert r.status_code == 201
    made = r.json()
    assert len(hooks_store.decode_secret(made["secret"])) == 32
    assert made["id"].startswith("hk_")
    assert (
        made["timeoutMs"] == 2000
        and made["failMode"] == "open"
        and made["context"] == 20
    )
    assert made["families"] == "*" and made["enabled"] is True

    for resp in (
        client.get("/api/admin/hooks", headers=root).json()[0],
        client.get(f"/api/admin/hooks/{made['id']}", headers=root).json(),
    ):
        assert "secret" not in resp
        assert resp["id"] == made["id"]
    assert hooks_store.get(made["id"]).secret == made["secret"]


def test_super_only(client: TestClient):
    admin = _user("a@example.com", "adm", "admin", "fam1")
    assert client.get("/api/admin/hooks", headers=admin).status_code == 403
    assert client.post("/api/admin/hooks", json=BODY, headers=admin).status_code == 403
    assert client.get("/api/admin/hooks").status_code in (401, 403)


def test_validation(client: TestClient, root):
    def post(**kw):
        return client.post(
            "/api/admin/hooks", json={**BODY, **kw}, headers=root
        ).status_code

    assert post(url="ftp://x/y") == 422
    assert post(url="nonsense") == 422
    assert post(timeoutMs=50) == 422
    assert post(timeoutMs=10001) == 422
    assert post(context=51) == 422
    assert post(mode="other") == 422
    assert post(families=[]) == 422
    assert post(failMode="maybe") == 422
    assert post(families=["f1"], context=0, timeoutMs=100) == 201


def test_eight_hook_limit(client: TestClient, root):
    for i in range(8):
        assert (
            client.post(
                "/api/admin/hooks", json={**BODY, "name": f"h{i}"}, headers=root
            ).status_code
            == 201
        )
    r = client.post("/api/admin/hooks", json=BODY, headers=root)
    assert r.status_code == 409
    first = client.get("/api/admin/hooks", headers=root).json()[0]["id"]
    assert client.delete(f"/api/admin/hooks/{first}", headers=root).status_code == 204
    assert client.post("/api/admin/hooks", json=BODY, headers=root).status_code == 201


def test_patch_rotate_delete(client: TestClient, root):
    made = client.post("/api/admin/hooks", json=BODY, headers=root).json()
    hid = made["id"]
    r = client.patch(
        f"/api/admin/hooks/{hid}", json={"enabled": False, "order": 5}, headers=root
    )
    assert r.status_code == 200
    assert (
        r.json()["enabled"] is False
        and r.json()["order"] == 5
        and r.json()["name"] == "guard"
    )
    assert (
        client.patch(
            f"/api/admin/hooks/{hid}", json={"timeoutMs": 5}, headers=root
        ).status_code
        == 422
    )
    assert (
        client.patch(
            "/api/admin/hooks/hk_nope", json={"order": 1}, headers=root
        ).status_code
        == 404
    )

    rot = client.post(f"/api/admin/hooks/{hid}/rotate-secret", headers=root).json()
    assert rot["secret"] != made["secret"]
    assert hooks_store.get(hid).secret == rot["secret"]

    assert client.delete(f"/api/admin/hooks/{hid}", headers=root).status_code == 204
    assert client.delete(f"/api/admin/hooks/{hid}", headers=root).status_code == 404
    assert client.get(f"/api/admin/hooks/{hid}", headers=root).status_code == 404


def test_family_disclosure_lists_covering_enabled_hooks(client: TestClient, root):
    fam = families_store.create_family(name="Examples", created_by="x")
    other = families_store.create_family(name="Other", created_by="x")
    admin = _user("fa@example.com", "fadm", "admin", fam.id)
    all_id = client.post(
        "/api/admin/hooks",
        json={**BODY, "name": "all", "url": "https://a.example.org:8443/x"},
        headers=root,
    ).json()["id"]
    client.post(
        "/api/admin/hooks",
        json={**BODY, "name": "mine", "mode": "processor", "families": [fam.id]},
        headers=root,
    )
    client.post(
        "/api/admin/hooks",
        json={**BODY, "name": "theirs", "families": [other.id]},
        headers=root,
    )
    off = client.post(
        "/api/admin/hooks", json={**BODY, "name": "off"}, headers=root
    ).json()
    client.patch(f"/api/admin/hooks/{off['id']}", json={"enabled": False}, headers=root)

    got = client.get("/api/family", headers=admin).json()["hooks"]
    assert sorted(h["name"] for h in got) == ["all", "mine"]
    by = {h["name"]: h for h in got}
    assert by["all"] == {
        "id": all_id,
        "name": "all",
        "mode": "filter",
        "host": "a.example.org:8443",
    }
    assert by["mine"]["mode"] == "processor"
    assert "secret" not in str(got)
