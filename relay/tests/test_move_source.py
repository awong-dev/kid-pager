# ruff: noqa: F811
"""The source side of the over-the-air move (docs/MOVE_DESIGN.md §4, §7.4, §8, §9, §12.5): preflight,
Freeze, the write guard, the bundle route, Start cutover, resend and cancel, Cancel move, the admin
status and the person's banner, the bridge answers and `hello`, and the frozen ingest path."""

from __future__ import annotations

import hashlib
import io
import json
import ssl
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from firebase_admin import auth as fb_auth

from app import deployexport, devauth, movekeys
from app.db.firestore import get_db
from app.fed import addr
from app.fed import client as fed_client
from app.ingest import Ingest
from app.move import hello_sig
from app.routing import Routing
from app.store import allow as allow_store
from app.store import backends as backends_store
from app.store import bridges as bridges_store
from app.store import device_secrets as device_secrets_store
from app.store import devices as devices_store
from app.store import families as families_store
from app.store import messages as messages_store
from app.store import users as users_store
from app.sysmsg import derived_id
from tests.bridge_world import World, broker, client, fcm, pair_bridge, world  # noqa: F401
from tests.conftest import up_topic
from tests.fake_transport import FakeBrokerClient
from tests.firebase_test_utils import auth_header

TARGET = "https://target.example/kid-pager"
SHA = "ab" * 32
PASS = "correct horse battery staple"
KEY = bytes(range(32))


class Target:
    """A stand-in for the target relay's `GET /api/public/move-info`."""

    def __init__(self) -> None:
        self.info: dict[str, Any] = {
            "v": 1,
            "authBackend": "home",
            "schemaVersion": 2,
            "relayVersion": "9.9",
            "baseUrl": TARGET,
            "broker": {"host": "target.example", "port": 8883},
            "ca": {"url": f"{TARGET}/ca/{SHA}.pem", "sha": SHA},
            "beam": None,
            "empty": True,
            "move": None,
        }
        self.down = False
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.down:
            raise httpx.ConnectError("down")
        if request.url.path != "/kid-pager/api/public/move-info":
            return httpx.Response(404)
        return httpx.Response(200, json=self.info)

    def imported(self, move_id: str) -> None:
        self.info["move"] = {"id": move_id, "imported": True}
        self.info["empty"] = False


@pytest.fixture
def target() -> Target:
    t = Target()
    fed_client.set_default_client(
        fed_client.FedClient(
            transport=httpx.MockTransport(t.handler),
            policy=addr.AddressPolicy(cloud=False, resolver=lambda _h, _p: ["93.184.216.34"]),
        )
    )
    return t


def account(uid: str, role: str, fam: str, **kw: Any) -> dict[str, str]:
    fb_auth.create_user(uid=uid, email=f"{uid}@example.com")
    users_store.create_user(uid=uid, alias=uid, display_name=uid.title(), role=role, family_id=fam, **kw)  # type: ignore[arg-type]
    fb_auth.set_custom_user_claims(uid, {"role": role, "fam": fam})
    return auth_header(uid)


def hmac_device(device_id: str, owner: str, fam: str, *, mvc: bool = True, mb: int | None = 1024):
    devices_store.create_device(
        device_id=device_id,
        owner_uid=owner,
        label=f"pager {device_id}",
        mqtt_username=device_id,
        mqtt_password_hash="x",
        auth_mode="hmac",
        family_id=fam,
    )
    device_secrets_store.create(device_id, hmac_key=KEY, mqtt_password_hash="y")
    backends_store.create_backend(owner, kind="pager", config={"deviceId": device_id}, enabled=True)
    devices_store.update_status(
        device_id, state="online", mb=mb, mvc=1 if mvc else None, tls="unpinned"
    )


class Ctx:
    def __init__(self, world: World, client: TestClient, broker: FakeBrokerClient, target: Target):
        self.world, self.client, self.broker, self.target = world, client, broker, target
        self.fam = world.family_id
        self.sent_id = ""
        self.other = families_store.create_family(name="Other", created_by="root").id
        self.sup = account("sup", "super", self.fam)
        self.kid = auth_header("kid")
        self.mom = account("mom", "member", self.other)
        hmac_device("pgr-a", "kid", self.fam)
        hmac_device("pgr-old", "kid", self.fam, mvc=False)
        hmac_device("pgr-b", "mom", self.other)

    def post(self, path: str, body: Any = None, headers: dict[str, str] | None = None):
        return self.client.post(path, json=body, headers=headers or self.sup)

    def freeze_body(self, **kw: Any) -> dict[str, Any]:
        body: dict[str, Any] = {
            "targetBaseUrl": TARGET,
            "confirm": {"host": "target.example", "port": 8883, "caSha": SHA},
            "accounts": [],
        }
        body.update(kw)
        return body

    def freeze(self, **kw: Any) -> str:
        r = self.post("/api/admin/move/freeze", self.freeze_body(**kw))
        assert r.status_code == 200, r.text
        return r.json()["moveId"]

    def published(self) -> list[dict[str, Any]]:
        return [json.loads(p.payload) for p in self.broker.published]

    def device(self, device_id: str) -> devices_store.Device:
        dev = devices_store.get_device(device_id)
        assert dev is not None
        return dev


@pytest.fixture
def ctx(world: World, client: TestClient, broker: FakeBrokerClient, target: Target) -> Ctx:
    return Ctx(world, client, broker, target)


def record() -> dict[str, Any]:
    snap = get_db().collection("settings").document("move").get()
    assert snap.exists
    return snap.to_dict()


# ---- preflight --------------------------------------------------------------------------------


def test_preflight_returns_move_info_and_fingerprint(ctx: Ctx):
    r = ctx.post("/api/admin/move/preflight", {"targetBaseUrl": TARGET + "/"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["authBackend"] == "home" and body["broker"] == {"host": "target.example", "port": 8883}
    assert body["caFingerprint"] == SHA and body["empty"] is True and body["move"] is None
    assert ctx.target.requests[0].url.path == "/kid-pager/api/public/move-info"
    assert "authorization" not in ctx.target.requests[0].headers


def test_preflight_is_super_only(ctx: Ctx):
    assert ctx.post("/api/admin/move/preflight", {"targetBaseUrl": TARGET}, ctx.kid).status_code == 403
    r = ctx.client.post("/api/admin/move/preflight", json={"targetBaseUrl": TARGET})
    assert r.status_code == 401


@pytest.mark.parametrize(
    "url", ["http://target.example", "https://user:pw@target.example", "https://t.example/a?b=1", "nope"]
)
def test_preflight_refuses_unusable_addresses(ctx: Ctx, url: str):
    assert ctx.post("/api/admin/move/preflight", {"targetBaseUrl": url}).status_code == 422
    assert ctx.target.requests == []


def test_preflight_applies_the_outbound_address_rules(ctx: Ctx):
    fed_client.set_default_client(
        fed_client.FedClient(
            transport=httpx.MockTransport(ctx.target.handler),
            policy=addr.AddressPolicy(cloud=False, resolver=lambda _h, _p: ["127.0.0.1"]),
        )
    )
    r = ctx.post("/api/admin/move/preflight", {"targetBaseUrl": TARGET})
    assert r.status_code == 422 and "loopback" in r.json()["detail"]
    assert ctx.target.requests == []


def test_preflight_target_down_and_bad_answers(ctx: Ctx):
    ctx.target.down = True
    assert ctx.post("/api/admin/move/preflight", {"targetBaseUrl": TARGET}).status_code == 502
    ctx.target.down = False
    ctx.target.info["baseUrl"] = "https://elsewhere.example"
    assert ctx.post("/api/admin/move/preflight", {"targetBaseUrl": TARGET}).status_code == 502
    ctx.target.info["baseUrl"] = TARGET
    ctx.target.info["broker"]["host"] = "bad host"
    assert ctx.post("/api/admin/move/preflight", {"targetBaseUrl": TARGET}).status_code == 502


def test_public_get_only_reaches_public_routes():
    client = fed_client.FedClient(policy=addr.AddressPolicy(cloud=False, resolver=lambda _h, _p: ["93.184.216.34"]))
    with pytest.raises(fed_client.FedTransportError):
        client.public_get("/api/admin/move", host="target.example")


# ---- a target with its own CA ----------------------------------------------------------------


class VerifySpy(fed_client.FedClient):
    """Records the TLS verification each request was made with."""

    def __init__(self, handler: Callable[[httpx.Request], httpx.Response]) -> None:
        super().__init__(
            transport=httpx.MockTransport(handler),
            policy=addr.AddressPolicy(cloud=False, resolver=lambda _h, _p: ["93.184.216.34"]),
        )
        self.verifies: list[Any] = []

    def _request_once(self, method, ip, port, base_path, path, headers, body, server_name, verify):
        self.verifies.append(verify)
        return super()._request_once(method, ip, port, base_path, path, headers, body, server_name, verify)


@pytest.fixture
def private_ca(ctx: Ctx) -> tuple[str, VerifySpy]:
    from tests.test_fed_client import make_ca, pem

    spy = VerifySpy(ctx.target.handler)
    fed_client.set_default_client(spy)
    return pem(make_ca("target CA")[1]), spy


def test_preflight_trusts_only_the_given_ca(ctx: Ctx, private_ca):
    ca, spy = private_ca
    r = ctx.post("/api/admin/move/preflight", {"targetBaseUrl": TARGET, "caPem": ca})
    assert r.status_code == 200, r.text
    (verify,) = spy.verifies
    assert isinstance(verify, ssl.SSLContext) and len(verify.get_ca_certs()) == 1
    assert verify.check_hostname and verify.verify_mode == ssl.CERT_REQUIRED
    ctx.post("/api/admin/move/preflight", {"targetBaseUrl": TARGET})
    assert spy.verifies[-1] is True


@pytest.mark.parametrize("bad", ["not a certificate", "-----BEGIN CERTIFICATE-----\nzzzz\n-----END CERTIFICATE-----\n"])
def test_preflight_and_freeze_refuse_a_ca_without_a_certificate(ctx: Ctx, bad: str):
    assert ctx.post("/api/admin/move/preflight", {"targetBaseUrl": TARGET, "caPem": bad}).status_code == 422
    assert ctx.post("/api/admin/move/freeze", ctx.freeze_body(caPem=bad)).status_code == 422
    assert ctx.target.requests == [] and get_db().collection("settings").document("move").get().exists is False


def test_an_empty_ca_is_no_ca(ctx: Ctx):
    assert ctx.post("/api/admin/move/preflight", {"targetBaseUrl": TARGET, "caPem": "  "}).status_code == 200
    ctx.freeze(caPem="")
    assert record()["target"]["tlsCa"] is None


def test_a_tls_failure_says_which_ca_is_missing(ctx: Ctx, private_ca):
    ca, _spy = private_ca

    def refuse(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("x", request=_request) from ssl.SSLError("certificate verify failed")

    fed_client.set_default_client(VerifySpy(refuse))
    r = ctx.post("/api/admin/move/preflight", {"targetBaseUrl": TARGET})
    assert r.status_code == 502 and "caPem" in r.json()["detail"]
    r = ctx.post("/api/admin/move/preflight", {"targetBaseUrl": TARGET, "caPem": ca})
    assert r.status_code == 502 and "does not match" in r.json()["detail"]


def test_the_ca_is_kept_and_used_for_every_later_call_to_the_target(ctx: Ctx, private_ca):
    ca, spy = private_ca
    move_id = ctx.freeze(caPem=ca)
    assert record()["target"]["tlsCa"] == ca
    spy.verifies.clear()
    # the status poll, then the cutover's imported check
    body = ctx.client.get("/api/admin/move", headers=ctx.sup).json()
    assert body["target"]["privateCa"] is True and "tlsCa" not in body["target"]
    ctx.target.imported(move_id)
    assert ctx.post("/api/admin/move/cutover", {}).status_code == 200
    assert len(spy.verifies) == 2
    assert all(isinstance(v, ssl.SSLContext) and len(v.get_ca_certs()) == 1 for v in spy.verifies)


def test_a_target_without_a_private_ca_reports_none(ctx: Ctx):
    ctx.freeze()
    assert ctx.client.get("/api/admin/move", headers=ctx.sup).json()["target"]["privateCa"] is False


# ---- Freeze -----------------------------------------------------------------------------------


def test_freeze_home_target(ctx: Ctx):
    move_id = ctx.freeze(accounts=[{"uid": "kid", "password": "a long password"}])
    assert movekeys.is_valid_move_id(move_id)
    rec = record()
    assert rec["id"] == move_id and rec["role"] == "source" and rec["state"] == "frozen"
    assert rec["bridgeRedirect"] == TARGET
    assert rec["target"] == {
        "baseUrl": TARGET,
        "authBackend": "home",
        "host": "target.example",
        "port": 8883,
        "caUrl": f"{TARGET}/ca/{SHA}.pem",
        "caSha": SHA,
        "tlsCa": None,
    }
    for fid in (ctx.fam, ctx.other):
        assert families_store.is_frozen(fid)
    # claim codes for everyone with a sign-in; the admin-set password is stored as a hash only
    claims = {s.id: s.to_dict() for s in get_db().collection("moveClaims").stream()}
    assert set(claims) == {"adm", "kid", "mom", "sup"}
    assert all(c["code"] and c["digest"] and c["moveId"] == move_id for c in claims.values())
    assert claims["kid"]["passwordHash"].startswith("$argon2id$")
    assert "a long password" not in json.dumps(claims)
    assert claims["mom"]["passwordHash"] is None
    # every pager's move row
    assert ctx.device("pgr-a").move.model_dump(include={"id", "capable"}) == {"id": move_id, "capable": True}
    assert ctx.device("pgr-old").move.capable is False


def test_freeze_is_super_only(ctx: Ctx):
    assert ctx.post("/api/admin/move/freeze", ctx.freeze_body(), ctx.kid).status_code == 403
    assert not families_store.is_frozen(ctx.fam)


def test_freeze_409_when_move_info_changed(ctx: Ctx):
    for change in (
        lambda i: i["broker"].update(port=8884),
        lambda i: i["broker"].update(host="other.example"),
        lambda i: i.update(ca=None),
        lambda i: i["ca"].update(sha="cd" * 32, url=f"{TARGET}/ca/{'cd' * 32}.pem"),
    ):
        change(ctx.target.info)
        r = ctx.post("/api/admin/move/freeze", ctx.freeze_body())
        assert r.status_code == 409, r.text
        ctx.target.info = Target().info
    assert not families_store.is_frozen(ctx.fam)
    assert get_db().collection("settings").document("move").get().exists is False


def test_freeze_409_on_schema_difference_and_when_target_unreachable(ctx: Ctx):
    ctx.target.info["schemaVersion"] = 3
    assert ctx.post("/api/admin/move/freeze", ctx.freeze_body()).status_code == 409
    ctx.target.info["schemaVersion"] = 2
    ctx.target.down = True
    assert ctx.post("/api/admin/move/freeze", ctx.freeze_body()).status_code == 502
    assert not families_store.is_frozen(ctx.fam)


def test_freeze_twice_is_refused(ctx: Ctx):
    ctx.freeze()
    assert ctx.post("/api/admin/move/freeze", ctx.freeze_body()).status_code == 409


def test_freeze_firebase_target_needs_every_answer(ctx: Ctx):
    ctx.target.info["authBackend"] = "firebase"
    users_store.update_user("adm", email="adm@example.org")
    users_store.update_user("sup", email="sup@example.org")
    r = ctx.post("/api/admin/move/freeze", ctx.freeze_body())
    assert r.status_code == 422
    assert r.json()["missing"] == ["kid", "mom"] and r.json()["detail"]
    assert not families_store.is_frozen(ctx.fam)

    accounts = [{"uid": "kid", "email": "Kid@Example.org"}, {"uid": "mom", "noSignIn": True}]
    move_id = ctx.freeze(accounts=accounts)
    assert users_store.get_user("kid").email == "kid@example.org"
    claims = {s.id: s.to_dict() for s in get_db().collection("moveClaims").stream()}
    assert claims["mom"]["noSignIn"] is True and claims["mom"]["code"] is None
    assert claims["kid"]["email"] == "kid@example.org" and claims["kid"]["code"] is None
    assert record()["target"]["authBackend"] == "firebase" and record()["id"] == move_id


def test_freeze_firebase_target_refuses_a_duplicate_or_malformed_email(ctx: Ctx):
    ctx.target.info["authBackend"] = "firebase"
    users_store.update_user("adm", email="adm@example.org")
    users_store.update_user("sup", email="sup@example.org")
    both = [{"uid": "kid", "email": "same@example.org"}, {"uid": "mom", "email": "SAME@example.org"}]
    assert ctx.post("/api/admin/move/freeze", ctx.freeze_body(accounts=both)).status_code == 422
    bad = [{"uid": "kid", "email": "nope"}, {"uid": "mom", "noSignIn": True}]
    assert ctx.post("/api/admin/move/freeze", ctx.freeze_body(accounts=bad)).status_code == 422
    assert not families_store.is_frozen(ctx.fam)


def test_freeze_answer_checks_on_a_home_target(ctx: Ctx):
    short = [{"uid": "kid", "password": "short"}]
    assert ctx.post("/api/admin/move/freeze", ctx.freeze_body(accounts=short)).status_code == 422
    stray = [{"uid": "nobody"}]
    r = ctx.post("/api/admin/move/freeze", ctx.freeze_body(accounts=stray))
    assert r.status_code == 422 and r.json()["unknown"] == ["nobody"]
    both = [{"uid": "kid", "noSignIn": True, "password": "a long password"}]
    assert ctx.post("/api/admin/move/freeze", ctx.freeze_body(accounts=both)).status_code == 422
    assert not families_store.is_frozen(ctx.fam)


def test_freeze_refuses_a_broker_host_too_long_for_an_order(ctx: Ctx):
    ctx.target.info["broker"]["host"] = "a" * 130
    r = ctx.post("/api/admin/move/freeze", ctx.freeze_body(confirm={"host": "a" * 130, "port": 8883, "caSha": SHA}))
    assert r.status_code == 502


# ---- the write guard --------------------------------------------------------------------------


def test_every_write_is_refused_while_frozen_for_move(ctx: Ctx):
    ctx.freeze()
    for headers in (ctx.kid, ctx.sup):
        r = ctx.client.post("/api/conversations/mom/messages", json={"body": "hi"}, headers=headers)
        assert r.status_code == 503 and r.json() == {"detail": "moving"}
    r = ctx.client.patch("/api/family/settings", json={"linkChecks": False}, headers=ctx.sup)
    assert r.status_code == 503 and r.json() == {"detail": "moving"}
    r = ctx.client.post("/api/admin/families/" + ctx.fam + "/unfreeze", headers=ctx.sup)
    assert r.status_code == 503
    # reads, the Move routes and sign-in answers still work
    assert ctx.client.get("/api/me", headers=ctx.kid).status_code == 200
    assert ctx.client.get("/api/admin/move", headers=ctx.sup).status_code == 200
    assert ctx.post("/api/admin/move/preflight", {"targetBaseUrl": TARGET}).status_code == 200
    assert ctx.client.post("/api/auth/logout", headers={**ctx.kid, "X-Requested-With": "pager"}).status_code != 503


def test_writes_resume_after_cancel(ctx: Ctx):
    ctx.freeze()
    assert ctx.post("/api/admin/move/cancel").status_code == 200
    r = ctx.client.patch("/api/family/settings", json={"linkChecks": False}, headers=ctx.sup)
    assert r.status_code != 503


def test_writes_are_not_refused_without_a_move(ctx: Ctx):
    r = ctx.client.patch("/api/family/settings", json={"linkChecks": False}, headers=ctx.sup)
    assert r.status_code != 503


# ---- the bundle -------------------------------------------------------------------------------


def _open(content: bytes) -> deployexport.KpdepFile:
    return deployexport.KpdepFile(io.BytesIO(content), PASS)


def test_bundle_route(ctx: Ctx):
    assert ctx.client.get("/api/admin/move/bundle", headers={**ctx.sup, "X-Export-Passphrase": PASS}).status_code == 409
    move_id = ctx.freeze(accounts=[{"uid": "kid", "password": "a long password"}])
    assert ctx.client.get("/api/admin/move/bundle", headers=ctx.sup).status_code == 400
    forbidden = ctx.client.get("/api/admin/move/bundle", headers={**ctx.kid, "X-Export-Passphrase": PASS})
    assert forbidden.status_code == 403

    r = ctx.client.get("/api/admin/move/bundle", headers={**ctx.sup, "X-Export-Passphrase": PASS})
    assert r.status_code == 200 and ".kpdep" in r.headers["content-disposition"]
    src = _open(r.content)
    m = src.manifest()
    assert m.move.id == move_id and m.move.target.baseUrl == TARGET and m.move.target.authBackend == "home"
    assert sorted(f.id for f in m.families) == sorted([ctx.fam, ctx.other])
    text = b"".join(src.lines()).decode()
    claims = {s.id: s.to_dict() for s in get_db().collection("moveClaims").stream()}
    for uid, c in claims.items():
        assert f"inviteCodes/{c['digest']}" in text, uid
        assert c["code"].replace("-", "") not in text
    assert claims["kid"]["passwordHash"] in text and '"mustChange":true' in text
    _k_prime, pw_prime = movekeys.derive(KEY, move_id, "pgr-a")
    assert pw_prime in text and KEY.hex() not in text
    # the bundle can be fetched again
    again = ctx.client.get("/api/admin/move/bundle", headers={**ctx.sup, "X-Export-Passphrase": PASS})
    assert again.status_code == 200 and _open(again.content).manifest().move.id == move_id


def test_bundle_for_a_firebase_target_names_no_sign_in_people(ctx: Ctx):
    ctx.target.info["authBackend"] = "firebase"
    users_store.update_user("adm", email="adm@example.org")
    users_store.update_user("sup", email="sup@example.org")
    ctx.freeze(accounts=[{"uid": "kid", "email": "kid@example.org"}, {"uid": "mom", "noSignIn": True}])
    r = ctx.client.get("/api/admin/move/bundle", headers={**ctx.sup, "X-Export-Passphrase": PASS})
    m = _open(r.content).manifest()
    assert m.noSignIn == ["mom"] and m.move.target.authBackend == "firebase"
    text = b"".join(_open(r.content).lines()).decode()
    assert "inviteCodes/" not in text and "kid@example.org" in text


# ---- cutover ----------------------------------------------------------------------------------


def test_cutover_is_refused_until_the_target_has_imported(ctx: Ctx):
    move_id = ctx.freeze()
    assert ctx.post("/api/admin/move/cutover", {}).status_code == 409
    ctx.target.info["move"] = {"id": "mv_otherother1", "imported": True}
    assert ctx.post("/api/admin/move/cutover", {}).status_code == 409
    ctx.target.info["move"] = {"id": move_id, "imported": False}
    assert ctx.post("/api/admin/move/cutover", {}).status_code == 409
    ctx.target.down = True
    assert ctx.post("/api/admin/move/cutover", {}).status_code == 502
    assert ctx.broker.published == [] and record()["state"] == "frozen"


def test_cutover_orders_every_capable_pager(ctx: Ctx):
    move_id = ctx.freeze()
    ctx.target.imported(move_id)
    r = ctx.post("/api/admin/move/cutover", {})
    assert r.status_code == 200 and r.json() == {"ordered": 2}
    assert record()["state"] == "cutover" and record()["cutoverAt"] is not None
    sent = {p.topic: json.loads(p.payload) for p in ctx.broker.published}
    assert set(sent) == {"pager/pgr-a/down", "pager/pgr-b/down"}
    order = sent["pager/pgr-a/down"]["cfg"]["mv"]
    assert order["id"] == move_id and order["host"] == "target.example" and order["port"] == 8883
    assert order["ca"]["url"] == f"{TARGET}/ca/{SHA}.pem"
    assert devauth.verify(KEY, "pager/pgr-a/down", ctx.broker.published[0].payload)[0]
    raw = get_db().collection("devices").document("pgr-a").get().to_dict()
    assert raw["pendingCfgMv"]["acked"] is False
    assert ctx.device("pgr-a").move.sentAt is not None and ctx.device("pgr-a").move.ackAt is None
    assert ctx.device("pgr-old").move.sentAt is None

    # a second press orders nobody again; the status shows what was sent
    ctx.broker.clear()
    assert ctx.post("/api/admin/move/cutover", {}).json() == {"ordered": 0}
    assert ctx.broker.published == []


def test_cutover_named_pagers_and_the_gate_after_it_started(ctx: Ctx):
    move_id = ctx.freeze()
    ctx.target.imported(move_id)
    assert ctx.post("/api/admin/move/cutover", {"deviceIds": ["pgr-a"]}).json() == {"ordered": 1}
    assert [p.topic for p in ctx.broker.published] == ["pager/pgr-a/down"]
    # the target is not asked again once the cutover has started
    ctx.target.down = True
    assert ctx.post("/api/admin/move/cutover", {"deviceIds": ["pgr-b"]}).json() == {"ordered": 1}
    # a pager that cannot move by itself is skipped; an unknown one is a 404
    assert ctx.post("/api/admin/move/cutover", {"deviceIds": ["pgr-old"]}).json() == {"ordered": 0}
    assert ctx.post("/api/admin/move/cutover", {"deviceIds": ["pgr-zz"]}).status_code == 404


def test_cutover_skips_a_pager_that_does_not_sign_with_a_key(ctx: Ctx):
    move_id = ctx.freeze()
    ctx.target.imported(move_id)
    get_db().collection("devices").document("pgr-b").update({"authMode": "password"})
    assert ctx.post("/api/admin/move/cutover", {}).json() == {"ordered": 1}


def test_cutover_needs_a_super_and_a_move(ctx: Ctx):
    assert ctx.post("/api/admin/move/cutover", {}, ctx.kid).status_code == 403
    assert ctx.post("/api/admin/move/cutover", {}).status_code == 409


def test_resend_and_cancel_one_pager(ctx: Ctx):
    move_id = ctx.freeze()
    ctx.target.imported(move_id)
    assert ctx.post("/api/admin/move/devices/pgr-a/resend").status_code == 409  # not started
    ctx.post("/api/admin/move/cutover", {})
    ctx.broker.clear()

    assert ctx.post("/api/admin/move/devices/pgr-a/resend").json() == {"ordered": 1}
    again = ctx.published()
    assert len(again) == 1 and again[0]["cfg"]["mv"]["id"] == move_id
    assert ctx.post("/api/admin/move/devices/pgr-old/resend").status_code == 409
    assert ctx.post("/api/admin/move/devices/nope/resend").status_code == 404

    ctx.broker.clear()
    assert ctx.post("/api/admin/move/devices/pgr-a/cancel").status_code == 200
    cancel = ctx.published()
    assert cancel[0]["cfg"]["mv"] == {"id": move_id, "cancel": True}
    assert ctx.device("pgr-a").move.cancelledAt is not None
    assert ctx.post("/api/admin/move/devices/pgr-old/cancel").status_code == 409  # never ordered
    assert ctx.post("/api/admin/move/devices/pgr-a/resend", None, ctx.kid).status_code == 403

    # the pager's ledger ignores an order whose id it cancelled: no resend, and cutover skips it
    ctx.broker.clear()
    assert ctx.post("/api/admin/move/devices/pgr-a/resend").status_code == 409
    assert ctx.post("/api/admin/move/cutover", {"deviceIds": ["pgr-a"]}).json() == {"ordered": 0}
    assert ctx.post("/api/admin/move/cutover", {}).json() == {"ordered": 0}
    assert ctx.published() == []
    assert ctx.device("pgr-a").move.cancelledAt is not None


# ---- Cancel move ------------------------------------------------------------------------------


def test_cancel_before_cutover(ctx: Ctx):
    move_id = ctx.freeze()
    assert ctx.post("/api/admin/move/cancel", None, ctx.kid).status_code == 403
    r = ctx.post("/api/admin/move/cancel")
    assert r.status_code == 200 and r.json() == {"cancelled": 0}
    rec = record()
    assert rec["id"] == move_id and rec["state"] == "cancelled" and rec["bridgeRedirect"] is None
    assert not families_store.is_frozen(ctx.fam) and not families_store.is_frozen(ctx.other)
    assert list(get_db().collection("moveClaims").stream()) == []
    assert ctx.broker.published == []
    assert ctx.post("/api/admin/move/cancel").status_code == 409
    assert ctx.post("/api/admin/move/cutover", {}).status_code == 409


def test_cancel_after_cutover_sends_a_cancel_to_every_ordered_pager(ctx: Ctx):
    move_id = ctx.freeze()
    ctx.target.imported(move_id)
    ctx.post("/api/admin/move/cutover", {"deviceIds": ["pgr-a"]})
    ctx.broker.clear()
    r = ctx.post("/api/admin/move/cancel")
    assert r.json() == {"cancelled": 1}
    assert [p.topic for p in ctx.broker.published] == ["pager/pgr-a/down"]
    assert ctx.published()[0]["cfg"]["mv"] == {"id": move_id, "cancel": True}
    assert record()["state"] == "cancelled" and not families_store.is_frozen(ctx.fam)


def test_cancel_leaves_a_family_frozen_by_hand_before_the_move(ctx: Ctx):
    families_store.set_frozen(ctx.other, True)
    ctx.freeze()
    assert record()["frozenByMove"] == [ctx.fam]
    assert ctx.post("/api/admin/move/cancel").status_code == 200
    assert not families_store.is_frozen(ctx.fam) and families_store.is_frozen(ctx.other)


def test_a_new_freeze_after_a_cancel_gets_a_new_id(ctx: Ctx):
    first = ctx.freeze()
    ctx.post("/api/admin/move/cancel")
    second = ctx.freeze()
    assert second != first and record()["state"] == "frozen"


# ---- status and banner ------------------------------------------------------------------------


def test_status_without_a_move(ctx: Ctx):
    r = ctx.client.get("/api/admin/move", headers=ctx.sup)
    assert r.status_code == 200 and r.json()["state"] == "none"
    assert ctx.client.get("/api/admin/move", headers=ctx.kid).status_code == 403


def test_status_rows(ctx: Ctx):
    move_id = ctx.freeze(accounts=[{"uid": "kid", "password": "a long password"}])
    pair_bridge(ctx.client, ctx.world)
    body = ctx.client.get("/api/admin/move", headers=ctx.sup).json()
    assert body["role"] == "source" and body["id"] == move_id and body["state"] == "frozen"
    assert body["target"]["baseUrl"] == TARGET and body["target"]["host"] == "target.example"
    assert body["targetImported"] is False
    rows = {r["deviceId"]: r for r in body["devices"]}
    assert rows["pgr-a"]["capable"] is True and rows["pgr-old"]["capable"] is False
    assert rows["pgr-a"]["owner"] == "kid" and rows["pgr-a"]["bearer"] == "direct"
    assert rows["pgr-a"]["sentAt"] is None and rows["pgr-a"]["lastSeenAt"] is not None
    assert rows["pgr-a"]["darkSince"] is None
    people = {p["uid"]: p for p in body["people"]}
    assert set(people) == {"adm", "kid", "mom", "sup"} and people["kid"]["alias"] == "kid"
    assert len(body["bridges"]) == 1 and body["bridges"][0]["redirected"] is True
    assert body["bridges"][0]["owner"] == "kid"

    ctx.target.imported(move_id)
    assert ctx.client.get("/api/admin/move", headers=ctx.sup).json()["targetImported"] is True
    ctx.target.down = True
    assert "targetImported" not in ctx.client.get("/api/admin/move", headers=ctx.sup).json()


def test_status_dark_since_follows_the_bearer(ctx: Ctx):
    ctx.freeze()
    old = {"$ts": int((time.time() - 30 * 60) * 1_000_000)}
    from app.db import sqlitestore

    for dev, tls in (("pgr-a", "unpinned"), ("pgr-b", "proxy")):
        get_db().collection("devices").document(dev).update(
            {"status.updatedAt": sqlitestore.from_wire(old), "status.tls": tls}
        )
    rows = {r["deviceId"]: r for r in ctx.client.get("/api/admin/move", headers=ctx.sup).json()["devices"]}
    assert rows["pgr-a"]["darkSince"] is not None  # direct: dark after 24 minutes
    assert rows["pgr-b"]["darkSince"] is None and rows["pgr-b"]["bearer"] == "beam"  # Beam: after 60


def test_banner_for_a_person(ctx: Ctx):
    assert ctx.client.get("/api/me/move", headers=ctx.kid).status_code == 404
    move_id = ctx.freeze()
    body = ctx.client.get("/api/me/move", headers=ctx.kid).json()
    assert body["targetBaseUrl"] == TARGET
    code = get_db().collection("moveClaims").document("kid").get().to_dict()["code"]
    assert body["claim"] == {"url": f"{TARGET}/login#invite={code}"} and "email" not in body
    mom = ctx.client.get("/api/me/move", headers=ctx.mom).json()
    assert mom["claim"]["url"] != body["claim"]["url"]
    assert ctx.client.get("/api/me/move").status_code == 401
    ctx.post("/api/admin/move/cancel")
    assert ctx.client.get("/api/me/move", headers=ctx.kid).status_code == 404
    assert move_id


def test_banner_names_the_email_on_a_firebase_target(ctx: Ctx):
    ctx.target.info["authBackend"] = "firebase"
    users_store.update_user("adm", email="adm@example.org")
    users_store.update_user("sup", email="sup@example.org")
    ctx.freeze(accounts=[{"uid": "kid", "email": "kid@example.org"}, {"uid": "mom", "noSignIn": True}])
    body = ctx.client.get("/api/me/move", headers=ctx.kid).json()
    assert body == {"targetBaseUrl": TARGET, "email": "kid@example.org"}
    assert ctx.client.get("/api/me/move", headers=ctx.mom).json() == {"targetBaseUrl": TARGET}


# ---- bridge answers ---------------------------------------------------------------------------


def _event(ev_id: str = "e1") -> dict[str, Any]:
    return {
        "id": ev_id,
        "source": "sms",
        "conversation": {"id": "c1", "isGroup": False},
        "sender": {"name": "Mom", "phone": "+12065550111"},
        "text": "hi",
        "ts": 1,
    }


def _outbox_item(bridge: bridges_store.Bridge) -> str:
    ref = get_db().collection("bridges").document(bridge.id).collection("outbox").document("ob1")
    ref.set(
        {
            "state": "pending",
            "source": "sms",
            "to": {"phone": "+12065550111"},
            "text": "send me",
            "createdAt": datetime.now(UTC),
            "msgId": None,
            "bid": None,
        }
    )
    return "ob1"


def test_bridge_answers_while_redirected(ctx: Ctx):
    bridge, h = pair_bridge(ctx.client, ctx.world)
    ob = _outbox_item(bridge)
    move_id = ctx.freeze()
    moved = {"url": TARGET, "id": move_id}

    r = ctx.client.post("/bridge/heartbeat", json={"status": {"battery": 10}}, headers=h)
    assert r.status_code == 200 and r.json() == {"pending": 0, "moved": moved}
    assert bridges_store.get(bridge.id).status.battery != 10  # not recorded

    r = ctx.client.get("/bridge/outbox", headers=h)
    assert r.status_code == 200 and r.json() == {"items": [], "moved": moved}

    r = ctx.client.post("/bridge/events", json={"events": [_event()]}, headers=h)
    assert r.status_code == 503 and r.json() == {"detail": "moved", "moved": moved}

    r = ctx.client.post(f"/bridge/outbox/{ob}/ack", json={"state": "sent"}, headers=h)
    assert r.status_code == 503 and r.json() == {"detail": "moved", "moved": moved}
    row = get_db().collection("bridges").document(bridge.id).collection("outbox").document(ob).get()
    assert row.to_dict()["state"] == "pending"

    # an unauthenticated request is still a 401, not a redirect
    assert ctx.client.post("/bridge/heartbeat", json={}).status_code == 401
    assert ctx.client.get("/bridge/outbox").status_code == 401


def test_bridge_answers_return_to_normal_after_cancel(ctx: Ctx):
    _bridge, h = pair_bridge(ctx.client, ctx.world)
    ctx.freeze()
    ctx.post("/api/admin/move/cancel")
    r = ctx.client.post("/bridge/heartbeat", json={"status": {"battery": 10}}, headers=h)
    assert r.status_code == 200 and "moved" not in r.json()
    assert ctx.client.get("/bridge/outbox", headers=h).json() == {"items": []}
    r = ctx.client.post("/bridge/events", json={"events": [_event()]}, headers=h)
    assert r.status_code == 200


def test_bridge_pair_is_unchanged_while_redirected(ctx: Ctx):
    ctx.freeze()
    row = bridges_store.create("kid", ctx.fam, "phone", "adm")
    code, _ = bridges_store.create_pair_code(row.id)
    r = ctx.client.post("/bridge/pair", json={"code": code, "caps": {"sms": True}})
    assert r.status_code == 200 and "moved" not in r.json()


def test_bridge_redirect_is_what_the_record_says(ctx: Ctx):
    _bridge, h = pair_bridge(ctx.client, ctx.world)
    ctx.freeze()
    from app import move

    move.patch_record(bridgeRedirect="https://elsewhere.example/base/")
    r = ctx.client.get("/bridge/outbox", headers=h)
    assert r.json()["moved"]["url"] == "https://elsewhere.example/base"


# ---- GET /bridge/hello ------------------------------------------------------------------------

VECTOR_TOKEN = "b_0123456789.test-secret"
VECTOR_HASH = "3f7a3cbb91d0d0017834072b515b9b3bf46761e5ed05063cea0db33fdef35b32"
VECTOR_SIG = "c987c28916b52dc0add4d1bbcd6fa99983b765d36944a0aec29d7ab709f5566d"
VECTOR_NONCE = "00112233445566778899aabbccddeeff"
VECTOR_MOVE = "mv_0123456789ab"


def _seed_vector(token_hash: str | None = VECTOR_HASH) -> None:
    get_db().collection("bridges").document("b_0123456789").set(
        {"ownerUid": "kid", "familyId": "f", "label": "p", "tokenHash": token_hash}
    )
    get_db().collection("settings").document("move").set(
        {"id": VECTOR_MOVE, "role": "source", "state": "cancelled", "bridgeRedirect": None}
    )


def test_hello_signature_matches_the_design_vector(client: TestClient):
    assert hello_sig(VECTOR_HASH, VECTOR_NONCE, "b_0123456789", VECTOR_MOVE) == VECTOR_SIG
    _seed_vector()
    r = client.get("/bridge/hello", params={"bridge": "b_0123456789", "nonce": VECTOR_NONCE})
    assert r.status_code == 200
    assert r.json() == {"bridgeId": "b_0123456789", "moveId": VECTOR_MOVE, "sig": VECTOR_SIG}
    assert "authorization" not in r.request.headers


def test_hello_is_served_after_a_cancel_with_the_same_id(ctx: Ctx):
    bridge, h = pair_bridge(ctx.client, ctx.world)
    move_id = ctx.freeze()
    ctx.post("/api/admin/move/cancel")
    r = ctx.client.get("/bridge/hello", params={"bridge": bridge.id, "nonce": VECTOR_NONCE})
    stored = bridges_store.get(bridge.id).tokenHash
    assert r.status_code == 200 and r.json()["moveId"] == move_id
    assert r.json()["sig"] == hello_sig(stored, VECTOR_NONCE, bridge.id, move_id)
    token = h["Authorization"].split(" ", 1)[1]
    assert stored == hashlib.sha256(token.encode()).hexdigest()


@pytest.mark.parametrize(
    "params",
    [
        {"bridge": "b_0123456789", "nonce": "0011"},
        {"bridge": "b_0123456789", "nonce": VECTOR_NONCE.upper()},
        {"bridge": "b/0123", "nonce": VECTOR_NONCE},
        {"bridge": "x" * 65, "nonce": VECTOR_NONCE},
        {"nonce": VECTOR_NONCE},
        {"bridge": "b_0123456789"},
    ],
)
def test_hello_rejects_malformed_requests(client: TestClient, params: dict[str, str]):
    _seed_vector()
    r = client.get("/bridge/hello", params=params)
    assert r.status_code == 400 and r.json() == {"detail": "bad request"}


def test_hello_answers_one_404_for_every_other_failure(client: TestClient):
    params = {"bridge": "b_0123456789", "nonce": VECTOR_NONCE}
    unavailable = {"detail": "unavailable"}
    # no move record on this relay
    get_db().collection("bridges").document("b_0123456789").set(
        {"ownerUid": "kid", "familyId": "f", "label": "p", "tokenHash": VECTOR_HASH}
    )
    r = client.get("/bridge/hello", params=params)
    assert r.status_code == 404 and r.json() == unavailable
    # an unknown bridge
    _seed_vector()
    r = client.get("/bridge/hello", params={**params, "bridge": "b_unknown"})
    assert r.status_code == 404 and r.json() == unavailable
    # a bridge with no token hash
    _seed_vector(token_hash=None)
    r = client.get("/bridge/hello", params=params)
    assert r.status_code == 404 and r.json() == unavailable


def test_hello_is_rate_limited_per_client(client: TestClient):
    _seed_vector()
    params = {"bridge": "b_0123456789", "nonce": VECTOR_NONCE}
    codes = [client.get("/bridge/hello", params=params).status_code for _ in range(21)]
    assert codes[:20] == [200] * 20 and codes[20] == 429


# ---- the frozen ingest path -------------------------------------------------------------------


class Pager:
    """Signs a pager's envelopes, with a rising `n`."""

    def __init__(self, ingest: Ingest, device_id: str) -> None:
        self.ingest, self.device_id, self.n = ingest, device_id, 0

    def _sign(self, suffix: str, obj: dict[str, Any], sign: Callable = devauth.sign_json) -> bytes:
        self.n += 1
        return sign(KEY, f"pager/{self.device_id}/{suffix}", {**obj, "n": self.n})

    def up(self, obj: dict[str, Any]) -> None:
        self.ingest.handle_up(up_topic(self.device_id), self._sign("up", obj))

    def status(self, obj: dict[str, Any], sign: Callable = devauth.sign_json) -> None:
        self.ingest.handle_status(
            f"pager/{self.device_id}/status", self._sign("status", {"v": 1, **obj}, sign)
        )


def _text(mid: str, body: str = "hi") -> dict[str, Any]:
    return {"v": 1, "id": mid, "ts": int(time.time()), "from": "kid", "to": "mom", "body": body, "ack": None}


def _ack(mid: str) -> dict[str, Any]:
    return {"v": 1, "id": mid, "ts": int(time.time()), "ack": "shown"}


def _message_count() -> int:
    """Messages stored beyond the one the fixture sent before the freeze."""
    return len(list(get_db().collection("messages").stream())) - 1


@pytest.fixture
def frozen(ctx: Ctx) -> tuple[Ctx, Ingest, Pager]:
    allow_store.set_edge("kid", "mom", message=True, locate=False)
    allow_store.set_edge("mom", "kid", message=True, locate=False)
    msg = Routing(ctx.broker).send(
        sender_uid="mom", recipient_alias="kid", kind="text", body="hello", origin_backend_kind="webapp"
    ).messages[0]
    ctx.sent_id = msg.id
    ctx.freeze()
    ctx.broker.clear()
    ingest = Ingest(ctx.broker, Routing(ctx.broker))
    return ctx, ingest, Pager(ingest, "pgr-a")


def test_frozen_text_gets_the_moving_receipt_and_is_not_stored(frozen):
    ctx, _ingest, pager = frozen
    pager.up(_text("u_91c0aa11"))
    assert _message_count() == 0
    assert len(ctx.broker.published) == 1
    rcpt = ctx.published()[0]
    assert rcpt["kind"] == "rcpt" and rcpt["ref"] == "u_91c0aa11"
    assert rcpt["st"] == "failed" and rcpt["rsn"] == "moving" and rcpt["ack"] is None
    assert rcpt["id"] == derived_id("rcpt", "pgr-a", "u_91c0aa11", "moving", "failed")
    assert devauth.verify(KEY, "pager/pgr-a/down", ctx.broker.published[0].payload)[0]
    # a re-sent text gets the same receipt id, and is still not stored
    pager.up(_text("u_91c0aa11"))
    assert ctx.published()[1]["id"] == rcpt["id"] and _message_count() == 0


def test_frozen_text_with_kind_msg_is_a_text_too(frozen):
    ctx, _ingest, pager = frozen
    pager.up({**_text("u_91c0aa12"), "kind": "msg"})
    assert ctx.published()[0]["rsn"] == "moving" and _message_count() == 0


def test_frozen_text_from_a_pager_without_mb_gets_the_system_reply(frozen):
    ctx, ingest, _pager = frozen
    hmac_device("pgr-nomb", "kid", ctx.fam, mb=None)
    Pager(ingest, "pgr-nomb").up(_text("u_91c0aa13"))
    sent = ctx.published()
    assert len(sent) == 1 and sent[0]["from"] == "system" and sent[0]["body"] == families_store.FROZEN_REPLY
    assert _message_count() == 0


def test_frozen_contact_req_gets_the_system_reply_and_sms_log_is_dropped(frozen):
    ctx, _ingest, pager = frozen
    pager.up({"v": 1, "id": "c_1abc2def", "ts": int(time.time()), "kind": "contact_req", "name": "Zed", "phone": "+12065550123"})
    sent = ctx.published()
    assert len(sent) == 1 and sent[0]["from"] == "system"
    ctx.broker.clear()
    pager.up({"v": 1, "id": "s_1abc2def", "ts": int(time.time()), "kind": "sms_log", "dir": "in", "phone": "+12065550123", "body": "x"})
    assert ctx.broker.published == []
    assert list(get_db().collection("contactRequests").stream()) == []
    assert list(get_db().collection("devices").document("pgr-a").collection("smsLog").stream()) == []


def test_frozen_ack_is_applied(frozen):
    ctx, _ingest, pager = frozen
    pager.up(_ack(ctx.sent_id))
    got = messages_store.get_message(ctx.sent_id)
    assert sorted(d.state for d in got.deliveries.values() if d.kind == "pager") == ["sent", "shown"]


def test_frozen_ack_of_the_order_stamps_its_row(frozen):
    ctx, _ingest, pager = frozen
    ctx.target.imported(record()["id"])
    ctx.post("/api/admin/move/cutover", {"deviceIds": ["pgr-a"]})
    order_id = ctx.published()[-1]["id"]
    assert ctx.device("pgr-a").move.ackAt is None
    pager.up(_ack(order_id))
    raw = get_db().collection("devices").document("pgr-a").get().to_dict()
    assert raw["pendingCfgMv"]["acked"] is True and ctx.device("pgr-a").move.ackAt is not None
    rows = {r["deviceId"]: r for r in ctx.client.get("/api/admin/move", headers=ctx.sup).json()["devices"]}
    assert rows["pgr-a"]["ackAt"] is not None
    # an ack of something else leaves the stamp alone
    pager.up(_ack("m_nonexist1"))


def test_frozen_ack_of_a_cancel_does_not_count_as_the_order_acked(frozen):
    ctx, _ingest, pager = frozen
    ctx.target.imported(record()["id"])
    ctx.post("/api/admin/move/cutover", {"deviceIds": ["pgr-a"]})
    ctx.post("/api/admin/move/devices/pgr-a/cancel")
    cancel_id = ctx.published()[-1]["id"]
    pager.up(_ack(cancel_id))
    assert ctx.device("pgr-a").move.ackAt is None


def test_frozen_status_is_processed_and_the_order_is_resent_on_every_online_edge(frozen):
    ctx, _ingest, pager = frozen
    ctx.target.imported(record()["id"])
    ctx.post("/api/admin/move/cutover", {"deviceIds": ["pgr-a"]})
    order_id = ctx.published()[-1]["id"]
    ctx.broker.clear()

    pager.status({"state": "offline", "session": "s_00000000"})
    assert ctx.device("pgr-a").status.state == "offline"
    pager.status({"state": "online", "mode": "normal", "ts": int(time.time()), "session": "s_00000001", "mb": 1024, "mvc": 1, "fw": "1.0"})
    assert ctx.device("pgr-a").status.state == "online"
    resent = [m for m in ctx.published() if m.get("kind") == "cfg"]
    assert [m["id"] for m in resent] == [order_id] and resent[0]["cfg"]["mv"]["host"] == "target.example"
    n_first = ctx.published()[-1]["n"]

    # another edge, another publish, signed afresh
    ctx.broker.clear()
    pager.status({"state": "offline", "session": "s_00000000"})
    pager.status({"state": "online", "mode": "normal", "ts": int(time.time()), "session": "s_00000002", "mb": 1024, "mvc": 1, "fw": "1.0"})
    again = [m for m in ctx.published() if m.get("kind") == "cfg"]
    assert [m["id"] for m in again] == [order_id] and again[0]["n"] != n_first

    # once acked it is not re-sent
    pager.up(_ack(order_id))
    ctx.broker.clear()
    pager.status({"state": "offline", "session": "s_00000000"})
    pager.status({"state": "online", "mode": "normal", "ts": int(time.time()), "session": "s_00000003", "mb": 1024, "mvc": 1, "fw": "1.0"})
    assert [m for m in ctx.published() if m.get("kind") == "cfg"] == []


def test_frozen_status_records_capability_and_the_pagers_report(frozen):
    ctx, _ingest, pager = frozen
    move_id = record()["id"]
    pager.status({"state": "online", "mode": "normal", "ts": int(time.time()), "session": "s_00000001", "mb": 1024, "fw": "1.0"})
    assert ctx.device("pgr-a").move.capable is False  # `mvc` went missing
    pager.status({"state": "online", "mode": "normal", "ts": int(time.time()), "session": "s_00000002", "mb": 1024, "mvc": 1,
                  "mv": {"id": move_id, "st": "fail", "rsn": "ca", "at": 2}})
    mv = ctx.device("pgr-a").move
    assert mv.capable is True
    assert (mv.last.st, mv.last.rsn, mv.last.at) == ("fail", "ca", 2) and mv.last.ts is not None
    # a report for another move is not recorded
    pager.status({"state": "online", "mode": "normal", "ts": int(time.time()), "session": "s_00000003", "mb": 1024, "mvc": 1,
                  "mv": {"id": "mv_otherother1", "st": "ok", "at": 1}})
    assert ctx.device("pgr-a").move.last.st == "fail"
    rows = {r["deviceId"]: r for r in ctx.client.get("/api/admin/move", headers=ctx.sup).json()["devices"]}
    assert rows["pgr-a"]["last"]["rsn"] == "ca" and rows["pgr-a"]["last"]["at"] == 2


def test_frozen_status_cbor_carries_the_move_report(frozen):
    ctx, _ingest, pager = frozen
    move_id = record()["id"]
    pager.status(
        {"state": "online", "mode": "normal", "ts": int(time.time()), "session": "s_00000001", "mb": 1024, "mvc": 1,
         "mv": {"id": move_id, "st": "try", "at": 1}},
        sign=devauth.sign_cbor,
    )
    assert ctx.device("pgr-a").move.last.st == "try"


def test_a_plain_family_freeze_keeps_todays_behaviour(ctx: Ctx):
    families_store.set_frozen(ctx.fam, True)
    ingest = Ingest(ctx.broker, Routing(ctx.broker))
    pager = Pager(ingest, "pgr-a")
    pager.up(_text("u_91c0aa14"))
    sent = ctx.published()
    assert len(sent) == 1 and sent[0]["from"] == "system"
    ctx.broker.clear()
    pager.status({"state": "online", "mode": "normal", "ts": int(time.time()), "session": "s_00000001", "mb": 1024, "mvc": 1})
    assert ctx.device("pgr-a").status.session != "s1"  # dropped
    assert ctx.broker.published == []


def test_frozen_loc_is_still_dropped(frozen):
    ctx, ingest, _pager = frozen
    n = 50
    payload = devauth.sign_json(KEY, "pager/pgr-a/loc", {"v": 1, "id": "l_1abc2def", "ts": int(time.time()), "lat": 1, "lon": 2, "n": n})
    ingest.handle_loc("pager/pgr-a/loc", payload)
    assert list(get_db().collection("locations").stream()) == []
    assert ctx.broker.published == []


def test_the_pagers_up_counter_still_advances_while_frozen(frozen):
    _ctx, _ingest, pager = frozen
    pager.up(_text("u_91c0aa15"))
    pager.up(_text("u_91c0aa16"))
    assert device_secrets_store.get("pgr-a").upN == 2


# ---- the frozen source's tick -----------------------------------------------------------------


def _attempts(msg_id: str) -> int:
    msg = messages_store.get_message(msg_id)
    assert msg is not None
    return sum(d.attempts for d in msg.deliveries.values() if d.kind == "pager")


def test_the_tick_of_a_frozen_source_leaves_undelivered_messages_alone(ctx: Ctx, monkeypatch):
    from app import bridge_health, jobs
    from app.fed import sender as fed_sender
    from app.tasks import InlineTaskQueue

    allow_store.set_edge("kid", "mom", message=True, locate=False)
    allow_store.set_edge("mom", "kid", message=True, locate=False)
    ctx.broker.fail_publish = True
    msg = Routing(ctx.broker).send(
        sender_uid="mom", recipient_alias="kid", kind="text", body="hello", origin_backend_kind="webapp"
    ).messages[0]
    ctx.broker.fail_publish = False
    before = _attempts(msg.id)
    calls = {"outbox": 0, "offline": 0, "health": 0}
    monkeypatch.setattr(jobs, "_fail_stale_bridge_outbox", lambda: calls.__setitem__("outbox", calls["outbox"] + 1) or 0)
    monkeypatch.setattr(fed_sender, "fail_offline", lambda **_k: calls.__setitem__("offline", calls["offline"] + 1) or 0)
    monkeypatch.setattr(bridge_health, "evaluate_all", lambda **_k: calls.__setitem__("health", calls["health"] + 1))

    move_id = ctx.freeze()
    ctx.broker.clear()
    routing = Routing(ctx.broker)
    result = jobs.tick(routing, task_queue=InlineTaskQueue(), broker=ctx.broker)
    assert result.retriesAttempted == 0 and ctx.broker.published == []
    assert _attempts(msg.id) == before
    assert calls == {"outbox": 0, "offline": 0, "health": 1}  # the rest of the tick still runs

    # a cancelled move lets the tick go back to retrying
    ctx.post("/api/admin/move/cancel")
    assert record()["state"] == "cancelled" and record()["id"] == move_id
    result = jobs.tick(routing, task_queue=InlineTaskQueue(), broker=ctx.broker)
    assert result.retriesAttempted >= 1
    assert calls["outbox"] == 1 and calls["offline"] == 1


# ---- an order the broker did not take ---------------------------------------------------------


def test_an_order_the_broker_did_not_accept_is_not_marked_sent(ctx: Ctx):
    move_id = ctx.freeze()
    ctx.target.imported(move_id)
    ctx.broker.fail_publish = True
    assert ctx.post("/api/admin/move/cutover", {}).json() == {"ordered": 0}
    assert ctx.device("pgr-a").move.sentAt is None and record()["state"] == "cutover"
    assert ctx.post("/api/admin/move/devices/pgr-a/resend").status_code == 502
    assert ctx.device("pgr-a").move.sentAt is None

    # once the broker takes it the pager is ordered; a second press then orders nobody again
    ctx.broker.fail_publish = False
    assert ctx.post("/api/admin/move/cutover", {}).json() == {"ordered": 2}
    assert ctx.device("pgr-a").move.sentAt is not None
    assert ctx.post("/api/admin/move/cutover", {}).json() == {"ordered": 0}


# ---- the cached move record -------------------------------------------------------------------


def test_the_guard_reads_the_move_record_once_for_a_few_seconds(ctx: Ctx, monkeypatch):
    from app import move

    reads = {"n": 0}
    real = move.get_record

    def counting():
        reads["n"] += 1
        return real()

    monkeypatch.setattr(move, "get_record", counting)
    move.invalidate_cache()
    for _ in range(5):
        assert move.frozen_for_move() is False
        assert move.delivery_held("pgr-a") is False
    assert reads["n"] == 1

    # this process's own writes show at once
    ctx.freeze()
    assert move.frozen_for_move() is True
    assert ctx.client.post("/api/me/anything", headers=ctx.kid).status_code == 503
    ctx.post("/api/admin/move/cancel")
    assert move.frozen_for_move() is False

    # and another instance's within the TTL
    now = [1000.0]
    monkeypatch.setattr(move.time, "monotonic", lambda: now[0])
    move.invalidate_cache()
    assert move.frozen_for_move() is False
    get_db().collection("settings").document("move").set(
        {"id": "mv_0123456789ab", "role": "source", "state": "frozen"}
    )
    assert move.frozen_for_move() is False
    now[0] += move.CACHE_TTL_S + 1
    assert move.frozen_for_move() is True
