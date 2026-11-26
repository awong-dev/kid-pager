"""Home sign-in (`AUTH_BACKEND=home`, set per test): first run, password/invite/passkey sign-in,
sessions, CSRF, rate limits, admin password and invite routes, `pager-admin`."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import cbor2
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url

from app import authn_home, homeadmin
from app.config import Settings
from app.db.firestore import get_db
from app.main import create_app
from app.store import families as families_store
from app.store import users as users_store
from tests.fake_transport import FakeBrokerClient

BASE_URL = "https://pager.example.test"
RP_ID = "pager.example.test"
CSRF = {"X-Requested-With": "pager"}
PASSWORD = "correct horse battery"


def make_settings(**overrides: object) -> Settings:
    defaults: dict[str, Any] = {
        "broker_api_url": "http://unused.invalid/api/v5",
        "broker_api_key": None,
        "broker_api_secret": None,
        "webhook_key": "test-webhook-key",
        "dev_mode": True,
        "google_cloud_project": None,
        "firestore_emulator_host": None,
        "firebase_auth_emulator_host": None,
        "public_base_url": BASE_URL,
    }
    defaults.update(overrides)
    return Settings(**defaults)


@pytest.fixture(autouse=True)
def _home(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTH_BACKEND", "home")
    for name in ("AUTH_IP", "AUTH_USER"):
        monkeypatch.delenv(f"RATE_LIMIT_{name}_LIMIT", raising=False)


@pytest.fixture
def client() -> Iterator[TestClient]:
    app = create_app(settings=make_settings(), broker_client=FakeBrokerClient())
    # https: the session cookie is `Secure`.
    with TestClient(app, base_url=BASE_URL) as c:
        yield c


def _first_admin(client: TestClient, username: str = "boss") -> dict:
    resp = client.post(
        "/api/setup/first-admin", json={"username": username, "password": PASSWORD}, headers=CSRF
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _member(alias: str, *, family_id: str = "default", role: str = "member") -> str:
    if families_store.get_family(family_id) is None:
        families_store.create_family(name=family_id, created_by="test", family_id=family_id)
    uid = authn_home.new_uid()
    users_store.create_user(
        uid=uid, alias=alias, display_name=alias.title(), role=role, family_id=family_id
    )
    return uid


def _bearer(uid: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {authn_home.create_session(uid, 'test')}"}


def test_first_run_creates_super_in_default_family_and_signs_in(client: TestClient):
    assert client.get("/api/setup/status").json() == {"needsSetup": True}

    resp = client.post(
        "/api/setup/first-admin", json={"username": "boss", "password": PASSWORD}, headers=CSRF
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["alias"] == "boss"
    cookie = resp.headers["set-cookie"]
    assert cookie.startswith("pager_session=")
    for attr in ("HttpOnly", "Secure", "SameSite=lax", "Path=/", "Max-Age=2592000"):
        assert attr.lower() in cookie.lower(), cookie

    user = users_store.get_user(body["uid"])
    assert user is not None and user.role == "super" and user.familyId == "default"
    assert families_store.get_family("default") is not None
    assert client.get("/api/setup/status").json() == {"needsSetup": False}

    me = client.get("/api/me")
    assert me.status_code == 200, me.text
    assert me.json()["role"] == "super"
    assert me.json()["authBackend"] == "home"
    assert me.json()["tlsMode"] == "public"
    assert me.json()["claimsStale"] is False


def test_first_admin_refused_once_users_exist(client: TestClient):
    _first_admin(client)
    resp = client.post(
        "/api/setup/first-admin", json={"username": "evil", "password": PASSWORD}, headers=CSRF
    )
    assert resp.status_code == 409
    assert users_store.get_user_by_alias("evil") is None


def test_first_admin_needs_csrf_header_and_a_real_password(client: TestClient):
    resp = client.post("/api/setup/first-admin", json={"username": "boss", "password": PASSWORD})
    assert resp.status_code == 403
    resp = client.post(
        "/api/setup/first-admin", json={"username": "boss", "password": "short"}, headers=CSRF
    )
    assert resp.status_code == 422
    assert client.get("/api/setup/status").json() == {"needsSetup": True}


def test_cookie_writes_need_the_csrf_header(client: TestClient):
    _first_admin(client)
    resp = client.patch("/api/me", json={"notify": {"alerts": False}})
    assert resp.status_code == 403
    assert "X-Requested-With" in resp.json()["detail"]
    resp = client.patch("/api/me", json={"notify": {"alerts": False}}, headers=CSRF)
    assert resp.status_code == 200, resp.text
    assert resp.json()["notify"]["alerts"] is False


def test_bearer_session_needs_no_csrf_header(client: TestClient):
    uid = _member("kid")
    headers = _bearer(uid)
    client.cookies.clear()
    assert client.get("/api/me", headers=headers).json()["user"]["uid"] == uid
    resp = client.patch("/api/me", json={"notify": {"alerts": False}}, headers=headers)
    assert resp.status_code == 200, resp.text


def test_no_session_is_401_and_bad_session_is_401(client: TestClient):
    assert client.get("/api/me").status_code == 401
    assert client.get("/api/me", headers={"Authorization": "Bearer nope"}).status_code == 401
    client.cookies.set("pager_session", "nope", domain=RP_ID)
    assert client.get("/api/me").status_code == 401


def test_registry_gate_and_disabled_still_apply(client: TestClient):
    stranger = authn_home.create_session("no-such-uid", None)
    resp = client.get("/api/me", headers={"Authorization": f"Bearer {stranger}"})
    assert resp.status_code == 403
    uid = _member("kid")
    users_store.update_user(uid, disabled=True)
    assert client.get("/api/me", headers=_bearer(uid)).status_code == 403


def test_role_and_family_come_from_the_user_document(client: TestClient):
    uid = _member("mom", role="admin")
    headers = _bearer(uid)
    resp = client.get("/api/family", headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["id"] == "default"
    # Demoted in the document: the next request sees it, no token refresh.
    users_store.update_user(uid, role="member")
    assert client.get("/api/family", headers=headers).status_code == 403


def test_idle_session_expires_and_is_deleted(client: TestClient):
    uid = _member("kid")
    token = authn_home.create_session(uid, None)
    ref = get_db().collection("sessions").document(hashlib.sha256(token.encode()).hexdigest())
    ref.update({"lastSeenAt": datetime.now(UTC) - timedelta(days=31)})
    resp = client.get("/api/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 401
    assert not ref.get().exists


def test_last_seen_written_at_most_once_a_minute(client: TestClient):
    uid = _member("kid")
    token = authn_home.create_session(uid, None)
    ref = get_db().collection("sessions").document(hashlib.sha256(token.encode()).hexdigest())
    recent = datetime.now(UTC) - timedelta(seconds=30)
    ref.update({"lastSeenAt": recent})
    client.get("/api/me", headers={"Authorization": f"Bearer {token}"})
    assert ref.get().to_dict()["lastSeenAt"] == recent
    stale = datetime.now(UTC) - timedelta(minutes=5)
    ref.update({"lastSeenAt": stale})
    client.get("/api/me", headers={"Authorization": f"Bearer {token}"})
    assert ref.get().to_dict()["lastSeenAt"] > stale + timedelta(minutes=4)


def test_session_stores_only_a_digest(client: TestClient):
    uid = _member("kid")
    token = authn_home.create_session(uid, "Agent/1.0")
    assert not get_db().collection("sessions").document(token).get().exists
    snap = get_db().collection("sessions").document(hashlib.sha256(token.encode()).hexdigest()).get()
    assert snap.to_dict()["uid"] == uid
    assert snap.to_dict()["userAgent"] == "Agent/1.0"


def test_login_logout_round_trip(client: TestClient):
    uid = _member("kid")
    authn_home.set_password(uid, PASSWORD)
    stored = get_db().collection("users").document(uid).collection("credentials").document("password")
    assert stored.get().to_dict()["hash"].startswith("$argon2id$")

    resp = client.post("/api/auth/login", json={"username": "kid", "password": PASSWORD}, headers=CSRF)
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"uid": uid, "alias": "kid"}
    session = client.get("/api/auth/session").json()
    assert session == {
        "uid": uid,
        "alias": "kid",
        "displayName": "Kid",
        "hasPassword": True,
        "passkeys": 0,
    }

    assert client.post("/api/auth/logout", headers=CSRF).status_code == 200
    assert "pager_session" not in client.cookies
    assert client.get("/api/auth/session").status_code == 401


def test_logout_deletes_the_server_session(client: TestClient):
    uid = _member("kid")
    headers = _bearer(uid)
    assert client.post("/api/auth/logout", headers={**headers, **CSRF}).status_code == 200
    assert client.get("/api/me", headers=headers).status_code == 401


def test_login_failures(client: TestClient):
    uid = _member("kid")
    authn_home.set_password(uid, PASSWORD)
    bad = client.post("/api/auth/login", json={"username": "kid", "password": "wrong!!!"}, headers=CSRF)
    assert bad.status_code == 401
    unknown = client.post(
        "/api/auth/login", json={"username": "ghost", "password": PASSWORD}, headers=CSRF
    )
    assert unknown.status_code == 401
    assert unknown.json()["detail"] == bad.json()["detail"]
    no_csrf = client.post("/api/auth/login", json={"username": "kid", "password": PASSWORD})
    assert no_csrf.status_code == 403
    users_store.update_user(uid, disabled=True)
    disabled = client.post(
        "/api/auth/login", json={"username": "kid", "password": PASSWORD}, headers=CSRF
    )
    assert disabled.status_code == 403


def test_login_rate_limited_per_username(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("RATE_LIMIT_AUTH_USER_LIMIT", "3")
    uid = _member("kid")
    authn_home.set_password(uid, PASSWORD)
    for _ in range(3):
        r = client.post("/api/auth/login", json={"username": "kid", "password": "nope-nope"}, headers=CSRF)
        assert r.status_code == 401
    r = client.post("/api/auth/login", json={"username": "kid", "password": PASSWORD}, headers=CSRF)
    assert r.status_code == 429
    # Another username from the same address is not affected.
    other = _member("sib")
    authn_home.set_password(other, PASSWORD)
    r = client.post("/api/auth/login", json={"username": "sib", "password": PASSWORD}, headers=CSRF)
    assert r.status_code == 200


def test_auth_rate_limited_per_ip(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("RATE_LIMIT_AUTH_IP_LIMIT", "2")
    hdr = {**CSRF, "X-Forwarded-For": "198.51.100.7"}
    assert client.post("/api/auth/invite/redeem", json={"code": "AAAA"}, headers=hdr).status_code == 401
    assert client.post("/api/auth/passkey/login/begin", headers=hdr).status_code == 200
    r = client.post("/api/auth/login", json={"username": "a", "password": "b"}, headers=hdr)
    assert r.status_code == 429
    other = {**CSRF, "X-Forwarded-For": "198.51.100.8"}
    assert client.post("/api/auth/passkey/login/begin", headers=other).status_code == 200


def test_family_admin_invite_then_redeem_with_password(client: TestClient):
    mom = _member("mom", role="admin")
    kid = _member("kid")
    resp = client.post(f"/api/family/people/{kid}/invite", headers=_bearer(mom))
    assert resp.status_code == 200, resp.text
    code = resp.json()["code"]
    assert len(code) == 14 and code.count("-") == 2
    expires = datetime.fromisoformat(resp.json()["expiresAt"])
    assert timedelta(days=6, hours=23) < expires - datetime.now(UTC) <= timedelta(days=7)

    client.cookies.clear()
    redeemed = client.post(
        "/api/auth/invite/redeem",
        json={"code": code.lower().replace("-", " "), "password": PASSWORD},
        headers=CSRF,
    )
    assert redeemed.status_code == 200, redeemed.text
    assert redeemed.json()["uid"] == kid
    assert client.get("/api/me").json()["user"]["uid"] == kid

    again = client.post("/api/auth/invite/redeem", json={"code": code}, headers=CSRF)
    assert again.status_code == 401
    client.cookies.clear()
    login = client.post("/api/auth/login", json={"username": "kid", "password": PASSWORD}, headers=CSRF)
    assert login.status_code == 200


def test_redeem_without_password_signs_in_only(client: TestClient):
    kid = _member("kid")
    code, _ = authn_home.mint_invite(kid, "test")
    resp = client.post("/api/auth/invite/redeem", json={"code": code}, headers=CSRF)
    assert resp.status_code == 200
    assert client.get("/api/auth/session").json()["hasPassword"] is False


def test_expired_invite_is_refused(client: TestClient):
    kid = _member("kid")
    code, _ = authn_home.mint_invite(kid, "test")
    for snap in get_db().collection("inviteCodes").stream():
        snap.reference.update({"expiresAt": datetime.now(UTC) - timedelta(seconds=1)})
    resp = client.post("/api/auth/invite/redeem", json={"code": code}, headers=CSRF)
    assert resp.status_code == 401


def test_invite_scope(client: TestClient):
    mom = _member("mom", role="admin")
    boss = _member("boss", role="super")
    outsider = _member("out", family_id="other")
    assert client.post(f"/api/family/people/{boss}/invite", headers=_bearer(mom)).status_code == 403
    assert client.post(f"/api/family/people/{outsider}/invite", headers=_bearer(mom)).status_code == 404
    kid = _member("kid")
    assert client.post(f"/api/family/people/{kid}/invite", headers=_bearer(kid)).status_code == 403
    resp = client.post(f"/api/family/people/{boss}/invite?family=default", headers=_bearer(boss))
    assert resp.status_code == 200


def test_super_sets_password(client: TestClient):
    boss = _member("boss", role="super")
    kid = _member("kid")
    resp = client.post(
        f"/api/admin/users/{kid}/password", json={"password": PASSWORD}, headers=_bearer(boss)
    )
    assert resp.status_code == 200, resp.text
    assert authn_home.verify_password(kid, PASSWORD)
    assert (
        client.post(
            f"/api/admin/users/{kid}/password", json={"password": PASSWORD}, headers=_bearer(kid)
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/api/admin/users/nobody/password", json={"password": PASSWORD}, headers=_bearer(boss)
        ).status_code
        == 404
    )


def test_create_user_needs_no_email_and_delete_removes_credentials(client: TestClient):
    boss = _member("boss", role="super")
    headers = _bearer(boss)
    resp = client.post(
        "/api/admin/users", json={"alias": "kid", "displayName": "Kid"}, headers=headers
    )
    assert resp.status_code == 200, resp.text
    uid = resp.json()["uid"]
    assert resp.json()["familyId"] == "default"
    authn_home.set_password(uid, PASSWORD)
    kid_headers = _bearer(uid)

    family_resp = client.post(
        "/api/family/members?family=default",
        json={"alias": "sib", "displayName": "Sib"},
        headers=headers,
    )
    assert family_resp.status_code == 200, family_resp.text

    assert client.delete(f"/api/admin/users/{uid}", headers=headers).status_code == 200
    creds = get_db().collection("users").document(uid).collection("credentials")
    assert list(creds.stream()) == []
    assert client.get("/api/me", headers=kid_headers).status_code == 401


def test_pager_admin_reset_password(client: TestClient, capsys: pytest.CaptureFixture[str]):
    _member("boss", role="super")
    assert homeadmin.main(["reset-password", "boss"]) == 0
    out = capsys.readouterr().out
    code = out.split("one-time code for boss: ")[1].split(" ")[0]
    resp = client.post(
        "/api/auth/invite/redeem", json={"code": code, "password": PASSWORD}, headers=CSRF
    )
    assert resp.status_code == 200
    assert homeadmin.main(["reset-password", "ghost"]) == 1


def test_dev_token_is_a_session(client: TestClient):
    uid = _member("kid")
    resp = client.post("/api/dev/token", json={"alias": "kid"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["uid"] == uid and body["kind"] == "session"
    client.cookies.clear()
    me = client.get("/api/me", headers={"Authorization": f"Bearer {body['token']}"})
    assert me.json()["user"]["uid"] == uid


def test_home_routes_absent_under_firebase(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AUTH_BACKEND", "firebase")
    app = create_app(settings=make_settings(), broker_client=FakeBrokerClient())
    with TestClient(app, base_url=BASE_URL) as c:
        assert c.get("/api/setup/status").status_code == 404
        assert c.post("/api/auth/login", json={}, headers=CSRF).status_code == 404


@pytest.mark.skipif(
    os.environ.get("DB_BACKEND", "").strip().lower() != "sqlite"
    or (os.environ.get("AUTH_BACKEND", "").strip().lower() or "firebase") != "home",
    reason="only AUTH_BACKEND=home DB_BACKEND=sqlite runs with no Firebase app",
)
def test_no_firebase_app_under_home_and_sqlite(client: TestClient):
    from app.db import firestore as firestore_module

    _first_admin(client)
    assert firestore_module._app is None


class SoftAuthenticator:
    """A software platform authenticator: P-256 resident credential, `none` attestation."""

    def __init__(self) -> None:
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.credential_id = os.urandom(16)
        self.sign_count = 0
        self.user_handle: bytes | None = None

    @staticmethod
    def _client_data(kind: str, challenge: str, origin: str) -> bytes:
        return json.dumps(
            {"type": kind, "challenge": challenge, "origin": origin, "crossOrigin": False}
        ).encode()

    def create(self, options: dict[str, Any], origin: str = BASE_URL) -> dict[str, Any]:
        assert options["authenticatorSelection"]["residentKey"] == "required"
        self.user_handle = base64url_to_bytes(options["user"]["id"])
        numbers = self.key.public_key().public_numbers()
        cose = cbor2.dumps(
            {1: 2, 3: -7, -1: 1, -2: numbers.x.to_bytes(32, "big"), -3: numbers.y.to_bytes(32, "big")}
        )
        auth_data = (
            hashlib.sha256(options["rp"]["id"].encode()).digest()
            + bytes([0x01 | 0x04 | 0x40])
            + self.sign_count.to_bytes(4, "big")
            + bytes(16)
            + len(self.credential_id).to_bytes(2, "big")
            + self.credential_id
            + cose
        )
        attestation = cbor2.dumps({"fmt": "none", "attStmt": {}, "authData": auth_data})
        cid = bytes_to_base64url(self.credential_id)
        return {
            "id": cid,
            "rawId": cid,
            "type": "public-key",
            "response": {
                "clientDataJSON": bytes_to_base64url(
                    self._client_data("webauthn.create", options["challenge"], origin)
                ),
                "attestationObject": bytes_to_base64url(attestation),
                "transports": ["internal", "bogus"],
            },
            "clientExtensionResults": {},
        }

    def get(
        self, options: dict[str, Any], origin: str = BASE_URL, *, bump: bool = True
    ) -> dict[str, Any]:
        assert not options.get("allowCredentials")  # discoverable: no username
        if bump:
            self.sign_count += 1
        client_data = self._client_data("webauthn.get", options["challenge"], origin)
        auth_data = (
            hashlib.sha256(options["rpId"].encode()).digest()
            + bytes([0x01 | 0x04])
            + self.sign_count.to_bytes(4, "big")
        )
        signature = self.key.sign(
            auth_data + hashlib.sha256(client_data).digest(), ec.ECDSA(hashes.SHA256())
        )
        cid = bytes_to_base64url(self.credential_id)
        assert self.user_handle is not None
        return {
            "id": cid,
            "rawId": cid,
            "type": "public-key",
            "response": {
                "clientDataJSON": bytes_to_base64url(client_data),
                "authenticatorData": bytes_to_base64url(auth_data),
                "signature": bytes_to_base64url(signature),
                "userHandle": bytes_to_base64url(self.user_handle),
            },
            "clientExtensionResults": {},
        }


def _register(client: TestClient, authenticator: SoftAuthenticator) -> str:
    begin = client.post("/api/auth/passkey/register/begin", headers=CSRF)
    assert begin.status_code == 200, begin.text
    options = begin.json()["options"]
    assert options["rp"]["id"] == RP_ID
    finish = client.post(
        "/api/auth/passkey/register/finish",
        json={"challengeId": begin.json()["challengeId"], "credential": authenticator.create(options)},
        headers=CSRF,
    )
    assert finish.status_code == 200, finish.text
    return finish.json()["credentialId"]


def _passkey_login(client: TestClient, authenticator: SoftAuthenticator, **kwargs: Any):
    begin = client.post("/api/auth/passkey/login/begin", headers=CSRF)
    assert begin.status_code == 200, begin.text
    credential = authenticator.get(begin.json()["options"], **kwargs)
    return client.post(
        "/api/auth/passkey/login/finish",
        json={"challengeId": begin.json()["challengeId"], "credential": credential},
        headers=CSRF,
    )


def test_passkey_register_then_sign_in(client: TestClient):
    admin = _first_admin(client)
    authenticator = SoftAuthenticator()
    credential_id = _register(client, authenticator)
    doc = (
        get_db().collection("users").document(admin["uid"]).collection("credentials")
        .document(credential_id).get().to_dict()
    )
    assert doc["kind"] == "passkey" and doc["signCount"] == 0
    assert doc["transports"] == ["internal"]
    assert client.get("/api/auth/session").json()["passkeys"] == 1

    client.post("/api/auth/logout", headers=CSRF)
    resp = _passkey_login(client, authenticator)
    assert resp.status_code == 200, resp.text
    assert resp.json()["uid"] == admin["uid"]
    assert client.get("/api/me").json()["role"] == "super"

    # Registering the same authenticator again is excluded by the options.
    begin = client.post("/api/auth/passkey/register/begin", headers=CSRF).json()
    assert [c["id"] for c in begin["options"]["excludeCredentials"]] == [credential_id]


def test_passkey_sign_count_must_advance(client: TestClient):
    _first_admin(client)
    authenticator = SoftAuthenticator()
    _register(client, authenticator)
    assert _passkey_login(client, authenticator).status_code == 200
    replay = _passkey_login(client, authenticator, bump=False)
    assert replay.status_code == 401
    assert "sign count" in replay.json()["detail"].lower()


def test_passkey_wrong_origin_and_unknown_credential(client: TestClient):
    _first_admin(client)
    authenticator = SoftAuthenticator()
    _register(client, authenticator)
    assert _passkey_login(client, authenticator, origin="https://evil.example").status_code == 401

    stranger = SoftAuthenticator()
    stranger.user_handle = authenticator.user_handle
    assert _passkey_login(client, stranger).status_code == 401


def test_passkey_challenge_is_single_use_and_bound_to_its_ceremony(client: TestClient):
    _first_admin(client)
    authenticator = SoftAuthenticator()
    begin = client.post("/api/auth/passkey/register/begin", headers=CSRF).json()
    credential = authenticator.create(begin["options"])
    body = {"challengeId": begin["challengeId"], "credential": credential}
    assert client.post("/api/auth/passkey/register/finish", json=body, headers=CSRF).status_code == 200
    assert client.post("/api/auth/passkey/register/finish", json=body, headers=CSRF).status_code == 400

    # A login challenge cannot finish a registration.
    login_begin = client.post("/api/auth/passkey/login/begin", headers=CSRF).json()
    other = SoftAuthenticator()
    reg = client.post("/api/auth/passkey/register/begin", headers=CSRF).json()
    crossed = {"challengeId": login_begin["challengeId"], "credential": other.create(reg["options"])}
    assert client.post("/api/auth/passkey/register/finish", json=crossed, headers=CSRF).status_code == 400


def test_passkey_registration_needs_a_session(client: TestClient):
    client.cookies.clear()
    assert client.post("/api/auth/passkey/register/begin", headers=CSRF).status_code == 401


def test_passkeys_unavailable_without_public_base_url():
    app = create_app(settings=make_settings(public_base_url=None), broker_client=FakeBrokerClient())
    with TestClient(app, base_url=BASE_URL) as c:
        assert c.post("/api/auth/passkey/login/begin", headers=CSRF).status_code == 503


@pytest.mark.parametrize(
    ("base", "origin"),
    [
        ("https://pager.example.test", "https://pager.example.test"),
        ("https://pager.example.test/", "https://pager.example.test"),
        ("https://pager.example.test/pager", "https://pager.example.test"),
        ("https://pager.example.test:8443/apps/pager/", "https://pager.example.test:8443"),
    ],
)
def test_relying_party_origin_has_no_path(base: str, origin: str):
    rp = authn_home.RelyingParty.from_base_url(base)
    assert rp.id == "pager.example.test" and rp.origin == origin


@pytest.mark.parametrize(("base_url", "path"), [(BASE_URL, "/"), (BASE_URL + "/apps/pager", "/apps/pager")])
def test_session_cookie_path_follows_the_base_path(base_url: str, path: str):
    app = create_app(settings=make_settings(public_base_url=base_url), broker_client=FakeBrokerClient())
    with TestClient(app, base_url=BASE_URL) as c:
        resp = c.post(
            "/api/setup/first-admin", json={"username": "boss", "password": PASSWORD}, headers=CSRF
        )
        assert resp.status_code == 200, resp.text
        assert f"path={path}".lower() in resp.headers["set-cookie"].lower().replace("; ", ";").split(";")
        out = c.post("/api/auth/logout", headers=CSRF)
        assert f"path={path}".lower() in out.headers["set-cookie"].lower().replace("; ", ";").split(";")
