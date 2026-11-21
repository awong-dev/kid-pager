"""Shared fixtures for the hook tests: a fake hook server over `httpx.MockTransport`
and a small two-family world."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field

import httpx
import pytest

from app import hooks as hooks_module
from app.routing import Routing
from app.store import allow as allow_store
from app.store import backends as backends_store
from app.store import devices as devices_store
from app.store import hooks as hooks_store
from app.store import users as users_store
from tests.fake_transport import FakeBrokerClient


@dataclass
class Call:
    url: str
    headers: dict[str, str]
    raw: bytes
    payload: dict


@dataclass
class FakeHooks:
    """Routes by URL host: `respond[host]` is a dict (200 JSON), a callable
    `(Call) -> httpx.Response`, or an Exception to raise."""

    calls: list[Call] = field(default_factory=list)
    respond: dict[str, object] = field(default_factory=dict)

    def handler(self, request: httpx.Request) -> httpx.Response:
        call = Call(
            url=str(request.url),
            headers={k.lower(): v for k, v in request.headers.items()},
            raw=request.content,
            payload=json.loads(request.content),
        )
        self.calls.append(call)
        r = self.respond.get(request.url.host, {"action": "allow"})
        if isinstance(r, Exception):
            raise r
        if callable(r):
            return r(call)
        return httpx.Response(200, json=r)

    def to(self, host: str) -> list[Call]:
        return [c for c in self.calls if httpx.URL(c.url).host == host]


@pytest.fixture
def fake_hooks() -> Iterator[FakeHooks]:
    fake = FakeHooks()
    hooks_module.set_client(httpx.Client(transport=httpx.MockTransport(fake.handler)))
    yield fake
    hooks_module.set_client(None)


def add_hook(host: str = "h1.test", *, mode: str = "filter", **kw) -> hooks_store.Hook:
    args = {
        "name": host,
        "url": f"http://{host}/hook",
        "mode": mode,
        "families": "*",
        "order": 0,
        "timeout_ms": 2000,
        "fail_mode": "open",
        "context": 20,
        "enabled": True,
        "created_by": "root",
        **kw,
    }
    return hooks_store.create(**args)


def make_user(uid: str, alias: str, family: str | None = None, **kw) -> None:
    users_store.create_user(
        uid=uid, alias=alias, display_name=alias.title(), family_id=family, **kw
    )


def befriend(a: str, b: str) -> None:
    allow_store.set_edge(a, b, message=True, locate=True)
    allow_store.set_edge(b, a, message=True, locate=True)


def pager_for(uid: str, device_id: str, *, default_to: str | None = None) -> None:
    devices_store.create_device(
        device_id=device_id,
        owner_uid=uid,
        label="d",
        mqtt_username=device_id,
        mqtt_password_hash="x",
        default_to_uid=default_to,
        auth_mode="password",
    )
    backends_store.create_backend(
        uid, kind="pager", config={"deviceId": device_id}, enabled=True
    )


@pytest.fixture
def routing() -> Routing:
    return Routing(FakeBrokerClient())


def web_send(routing: Routing, sender: str, alias: str, body: str, **kw):
    return routing.send(
        sender_uid=sender,
        recipient_alias=alias,
        kind="text",
        body=body,
        origin_backend_kind=kw.pop("origin_backend_kind", "webapp"),
        **kw,
    )


Responder = Callable[[Call], httpx.Response]
