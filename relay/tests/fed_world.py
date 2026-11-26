"""Several real relays in one process, each with its own SQLite database and app, joined by an
`httpx` transport that routes by host name. A relay made with `base_path="/pager"` is mounted
there the way the home server's nginx does it: the transport answers 404 for any path outside the
prefix and strips the prefix before the app sees the request, so a client that forgets it fails. Needs `DB_BACKEND=sqlite` and `AUTH_BACKEND=home`."""

from __future__ import annotations

import contextlib
import os
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app import authn_home, sysmsg
from app.config import Settings
from app.db import firestore as db_module
from app.db import sqlitestore
from app.fed import addr
from app.fed import client as client_mod
from app.fed import sender as fed_sender
from app.fed.client import FedClient
from app.main import create_app
from app.store import families as families_store
from app.store import fed_state
from app.store import hooks as hooks_store
from app.store import users as users_store
from tests.fake_transport import FakeBrokerClient

needs_two_relays = pytest.mark.skipif(
    os.environ.get("DB_BACKEND", "").strip().lower() != "sqlite"
    or os.environ.get("AUTH_BACKEND", "").strip().lower() != "home",
    reason="several relays in one process need DB_BACKEND=sqlite AUTH_BACKEND=home",
)


def fed_settings(**overrides: object) -> Settings:
    from tests.test_admin import make_settings

    return make_settings(**overrides)


@dataclass
class Hit:
    """One request as the network saw it: where it was sent, and what it claimed to be for."""

    method: str
    path: str  # as sent on the wire: with the peer's prefix, if it has one
    ip: str
    host_header: str
    sni: str | None
    headers: dict[str, str]
    status: int = 0


@dataclass
class Network:
    """The transport behind every relay's federation client."""

    relays: dict[str, Relay] = field(default_factory=dict)
    dns: dict[str, list[str]] = field(default_factory=dict)
    hits: list[Hit] = field(default_factory=list)
    down: set[str] = field(default_factory=set)
    refuse: dict[str, tuple[int, dict]] = field(default_factory=dict)

    def resolver(self, host: str, port: int) -> list[str]:
        return self.dns.get(host, ["127.0.0.1"])

    def client(self, *, cloud: bool = False) -> FedClient:
        return FedClient(
            transport=_Transport(self),
            policy=addr.AddressPolicy(
                cloud=cloud, resolver=self.resolver, allow_loopback=True
            ),
        )

    def to(self, host: str) -> list[Hit]:
        return [h for h in self.hits if h.host_header.split(":")[0] == host]

    def clear(self) -> None:
        self.hits.clear()


class _Transport(httpx.BaseTransport):
    def __init__(self, net: Network) -> None:
        self.net = net

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        request.read()
        host_header = request.headers["host"]
        host = host_header.split(":")[0]
        hit = Hit(
            method=request.method,
            path=request.url.raw_path.decode(),
            ip=request.url.host,
            host_header=host_header,
            sni=request.extensions.get("sni_hostname"),
            headers={k.lower(): v for k, v in request.headers.items()},
        )
        self.net.hits.append(hit)
        relay = self.net.relays.get(host)
        if relay is None or host in self.net.down:
            raise httpx.ConnectError(f"nothing at {host}", request=request)
        if host in self.net.refuse:
            status, body = self.net.refuse[host]
            hit.status = status
            return httpx.Response(status, json=body)
        prefix = relay.base_path
        route = hit.path
        if prefix:
            if hit.path != prefix and not hit.path.startswith(prefix + "/"):
                hit.status = 404  # nginx has no location for it
                return httpx.Response(404, json={"detail": "Not Found"})
            route = hit.path[len(prefix) :] or "/"
        headers = {k: v for k, v in request.headers.items() if k.lower() != "host"}
        with relay.active():
            resp = relay.http.request(
                request.method,
                f"https://{host_header}{route}",
                content=request.content,
                headers=headers,
            )
        hit.status = resp.status_code
        return httpx.Response(
            resp.status_code,
            headers={
                k: v
                for k, v in resp.headers.items()
                if k.lower() not in ("content-length", "content-encoding")
            },
            content=resp.content,
        )


class Relay:
    """One relay: a SQLite database, an app, a `TestClient`, a fake broker."""

    def __init__(
        self,
        net: Network,
        name: str,
        tmp_path: Path,
        *,
        host: str | None = None,
        base_path: str = "",
        **settings: object,
    ) -> None:
        self.net = net
        self.name = name
        self.host = host or f"{name}.example.org"
        self.base_path = base_path
        self.settings = fed_settings(
            public_base_url=f"https://{self.host}{base_path}", **settings
        )
        self.db = sqlitestore.open_client(str(tmp_path / f"{name}.sqlite"))
        self.broker = FakeBrokerClient()
        self._stack = contextlib.ExitStack()
        with self.active():
            self.app = create_app(settings=self.settings, broker_client=self.broker)
            self.http = self._stack.enter_context(TestClient(self.app))
        net.relays[self.host] = self
        self.tokens: dict[str, str] = {}


    @contextlib.contextmanager
    def active(self) -> Iterator[Relay]:
        prev_db = db_module._db
        prev_settings = fed_sender._settings
        db_module._db = self.db
        fed_sender.configure(self.settings)
        sysmsg.set_broker(self.broker)
        fed_state.invalidate_cache()
        hooks_store.invalidate_cache()
        try:
            yield self
        finally:
            db_module._db = prev_db
            fed_sender.configure(prev_settings)
            fed_state.invalidate_cache()
            hooks_store.invalidate_cache()

    def close(self) -> None:
        self._stack.close()
        self.db.close()


    def family(self, fid: str, name: str | None = None, *, federated: bool = True) -> str:
        with self.active():
            families_store.create_family(name=name or fid, created_by="root", family_id=fid)
            if federated:
                fed_state.set_family_enabled(fid, True)
        return fid

    def enable_server(self, on: bool = True) -> None:
        with self.active():
            fed_state.set_server_enabled(on, "test")

    def user(
        self,
        uid: str,
        alias: str,
        role: str,
        family: str,
        name: str | None = None,
    ) -> str:
        with self.active():
            users_store.create_user(
                uid=uid, alias=alias, display_name=name or alias, role=role, family_id=family
            )
            self.tokens[uid] = authn_home.create_session(uid, "pytest")
        return uid

    def sign_in(self, *uids: str) -> None:
        """Sessions for users that already exist here (an imported family's people)."""
        with self.active():
            for uid in uids:
                self.tokens[uid] = authn_home.create_session(uid, "pytest")

    def auth(self, uid: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.tokens[uid]}"}

    def api(
        self, method: str, path: str, uid: str, body: object = None, **kw: object
    ) -> httpx.Response:
        with self.active():
            return self.http.request(
                method, path, headers=self.auth(uid), json=body, **kw
            )

    def get(self, path: str, uid: str) -> httpx.Response:
        return self.api("GET", path, uid)

    def post(self, path: str, uid: str, body: object = None) -> httpx.Response:
        return self.api("POST", path, uid, body)

    def put(self, path: str, uid: str, body: object = None) -> httpx.Response:
        return self.api("PUT", path, uid, body)

    def delete(self, path: str, uid: str) -> httpx.Response:
        return self.api("DELETE", path, uid)

    def raw(self, method: str, path: str, *, content: bytes = b"", headers: dict | None = None):
        """An unauthenticated (federation) request straight at this relay."""
        with self.active():
            return self.http.request(
                method, f"https://{self.host}{path}", content=content, headers=headers or {}
            )

    def run(self, fn: Callable[[], object]) -> object:
        with self.active():
            return fn()


def link_families(a: Relay, a_admin: str, b: Relay, b_admin: str) -> tuple[dict, dict]:
    """Admin A invites, admin B pastes it; returns `(LinkOut on A, LinkOut on B)`."""
    invite = a.post("/api/family/fed/invites", a_admin).json()["invite"]
    on_b = b.post("/api/family/fed/links", b_admin, {"invite": invite})
    assert on_b.status_code == 201, on_b.text
    a_links = a.get("/api/family/fed", a_admin).json()["links"]
    assert len(a_links) >= 1
    return a_links[-1], on_b.json()


def now() -> int:
    return int(time.time())


@pytest.fixture
def net() -> Iterator[Network]:
    n = Network()
    client_mod.set_default_client(n.client())
    yield n
    client_mod.set_default_client(None)


@pytest.fixture
def make_relay(net: Network, tmp_path: Path) -> Iterator[Callable[..., Relay]]:
    made: list[Relay] = []

    def make(name: str, **kw: object) -> Relay:
        r = Relay(net, name, tmp_path, **kw)
        made.append(r)
        return r

    yield make
    for r in made:
        r.close()


def identity_of(relay: Relay, family_id: str):
    from app.store import fed_families

    with relay.active():
        return fed_families.get_or_create(family_id)


def signed_call(
    src: Relay,
    family_id: str,
    dst: Relay,
    to_fed_id: str,
    method: str,
    path: str,
    body: object = None,
    *,
    ts: int | None = None,
    private_key_b64: str | None = None,
    sender: str | None = None,
) -> httpx.Response:
    """A request from `src`'s family to `dst`'s, signed the way the real client signs it, sent
    straight to `dst` (for tests that forge or tamper)."""
    import json

    from app.fed import keys, sign

    ident = identity_of(src, family_id)
    raw = b"" if body is None else body if isinstance(body, bytes) else json.dumps(body).encode()
    headers = sign.sign(
        keys.private_from_b64(private_key_b64 or ident.privateKey),
        sender or ident.fedId,
        to_fed_id,
        method,
        path,
        raw,
        ts=ts,
    )
    if raw:
        headers["Content-Type"] = "application/json"
    return dst.raw(method, path, content=raw, headers=headers)
