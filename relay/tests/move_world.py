"""A source relay and a target relay in one process, each with its own SQLite database, for the
over-the-air move (docs/MOVE_DESIGN.md). The source is a cloud-like relay at the root of its host; the
target is a home relay mounted at `/kid-pager`, reached through the federation network so the source's
`move-info` request really goes to it. `SimPager` is a pager that signs its envelopes with `k` on the
source and, once it follows the order, with the derived `k'` on the target. Needs `DB_BACKEND=sqlite`
and `AUTH_BACKEND=home`."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from app import bridgeauth, devauth, movekeys, wire
from app.store import allow as allow_store
from app.store import backends as backends_store
from app.store import bridges as bridges_store
from app.store import device_secrets as device_secrets_store
from app.store import devices as devices_store
from tests.fed_world import Network, Relay
from tests.test_admin import FakeEmqxAdmin

PASS = "correct horse battery staple"
KEY = bytes(range(32))
PEM = "-----BEGIN CERTIFICATE-----\nTEST\n-----END CERTIFICATE-----\n"
SRC_HOST = "old.example.org"
DST_HOST = "new.example.org"
DST_URL = f"https://{DST_HOST}/kid-pager"
FAMILY = "fam"


def make_world(net: Network, make_relay: Callable[..., Relay], **target: object) -> MoveWorld:
    src = make_relay("src", host=SRC_HOST, broker_host=SRC_HOST)
    dst = make_relay(
        "dst",
        host=DST_HOST,
        base_path="/kid-pager",
        broker_host=DST_HOST,
        broker_ca_pem=PEM,
        **target,
    )
    for relay in (src, dst):
        relay.emqx = FakeEmqxAdmin()  # type: ignore[attr-defined]
        relay.app.state.emqx_admin = relay.emqx  # type: ignore[attr-defined]
    return MoveWorld(src, dst)


class MoveWorld:
    """Source people, one pager, one bridge; the target is empty."""

    def __init__(self, src: Relay, dst: Relay) -> None:
        self.src, self.dst = src, dst
        src.family(FAMILY, "Home", federated=False)
        src.user("sup", "admin", "super", FAMILY)
        src.user("mom", "mom", "member", FAMILY)
        src.user("kid", "kid", "member", FAMILY)
        with src.active():
            allow_store.set_edge("kid", "mom", message=True, locate=False)
            allow_store.set_edge("mom", "kid", message=True, locate=False)
        self.device("pgr-a", "kid")
        self.bridge_token = self.bridge("mom")

    # -- seeding

    def device(self, device_id: str, owner: str, *, key: bytes = KEY, mvc: bool = True) -> None:
        with self.src.active():
            devices_store.create_device(
                device_id=device_id,
                owner_uid=owner,
                label=f"pager {device_id}",
                mqtt_username=device_id,
                mqtt_password_hash="x",
                auth_mode="hmac",
                family_id=FAMILY,
            )
            device_secrets_store.create(device_id, hmac_key=key, mqtt_password_hash="y")
            backends_store.create_backend(
                owner, kind="pager", config={"deviceId": device_id}, enabled=True
            )
            devices_store.update_status(
                device_id, state="online", mb=1024, mvc=1 if mvc else None, tls="unpinned"
            )

    def bridge(self, owner: str) -> str:
        with self.src.active():
            b = bridges_store.create(owner, FAMILY, "phone", "sup")
            token, token_hash = bridgeauth.mint_token(b.id)
            bridges_store.set_token_hash(
                b.id, token_hash, caps=bridges_store.BridgeCaps(), status={}
            )
        self.bridge_id = b.id
        return token

    # -- the move

    def freeze(self, **kw: Any) -> str:
        pre = self.src.post("/api/admin/move/preflight", "sup", {"targetBaseUrl": DST_URL})
        assert pre.status_code == 200, pre.text
        info = pre.json()
        body = {
            "targetBaseUrl": DST_URL,
            "confirm": {
                "host": info["broker"]["host"],
                "port": info["broker"]["port"],
                "caSha": info["caFingerprint"],
            },
            "accounts": [],
        }
        body.update(kw)
        r = self.src.post("/api/admin/move/freeze", "sup", body)
        assert r.status_code == 200, r.text
        self.move_id = r.json()["moveId"]
        return self.move_id

    def bundle(self) -> bytes:
        with self.src.active():
            r = self.src.http.get(
                "/api/admin/move/bundle",
                headers={**self.src.auth("sup"), "X-Export-Passphrase": PASS},
            )
        assert r.status_code == 200, r.text
        return r.content

    def import_bundle(
        self, data: bytes, *, dry: bool = False, headers: dict[str, str] | None = None
    ):
        h = {"X-Export-Passphrase": PASS, **(headers or {})}
        path = "/api/admin/move/import" + ("?dryRun=1" if dry else "")
        return self.dst.raw("POST", path, content=data, headers=h)

    def moved_over(self) -> str:
        """Freeze, bundle, import: the target is live."""
        move_id = self.freeze()
        r = self.import_bundle(self.bundle())
        assert r.status_code == 201, r.text
        self.dst.sign_in("sup", "mom", "kid")
        return move_id

    def cutover(self) -> int:
        r = self.src.post("/api/admin/move/cutover", "sup", {})
        assert r.status_code == 200, r.text
        return r.json()["ordered"]

    def target_state(self, device_id: str) -> devices_store.Device:
        with self.dst.active():
            d = devices_store.get_device(device_id)
        assert d is not None
        return d


def published(relay: Relay, device_id: str, key: bytes) -> list[dict[str, Any]]:
    """What the relay sent to `device_id`, verified under `key` and decoded."""
    out = []
    for p in relay.broker.published:
        if p.topic != f"pager/{device_id}/down":
            continue
        ok, raw = devauth.verify(key, p.topic, p.payload)
        assert ok, "a /down that does not verify under the key"
        decoded = wire.decode_envelope_bytes(raw)
        assert decoded is not None
        out.append(decoded[0])
    return out


class SimPager:
    """Signs envelopes with a rising `n`, on whichever relay and key the caller names."""

    def __init__(self, device_id: str, k: bytes = KEY) -> None:
        self.device_id, self.k, self.n = device_id, k, 0
        self.k_prime: bytes | None = None
        self.pw_prime: str | None = None

    def derive(self, move_id: str) -> None:
        self.k_prime, self.pw_prime = movekeys.derive(self.k, move_id, self.device_id)

    def _sign(self, key: bytes, suffix: str, obj: dict[str, Any], cbor: bool) -> bytes:
        self.n += 1
        sign = devauth.sign_cbor if cbor else devauth.sign_json
        return sign(key, f"pager/{self.device_id}/{suffix}", {**obj, "n": self.n})

    def status(
        self,
        relay: Relay,
        key: bytes,
        *,
        session: str = "s_00000001",
        mv: dict[str, Any] | None = None,
        cbor: bool = False,
        mvc: bool = True,
        **extra: Any,
    ) -> int:
        """Publishes an online `/status`; returns the `n` it carried."""
        obj: dict[str, Any] = {
            "v": 1,
            "state": "online",
            "mode": "normal",
            "ts": int(time.time()),
            "session": session,
            "mb": 1024,
            "tls": "unpinned",
            **extra,
        }
        if mvc:
            obj["mvc"] = 1
        if mv is not None:
            obj["mv"] = mv
        payload = self._sign(key, "status", obj, cbor)
        with relay.active():
            relay.app.state.ingest.handle_status(f"pager/{self.device_id}/status", payload)
        return self.n

    def up(self, relay: Relay, key: bytes, obj: dict[str, Any], *, cbor: bool = False) -> None:
        payload = self._sign(key, "up", {"v": 1, "ts": int(time.time()), **obj}, cbor)
        with relay.active():
            relay.app.state.ingest.handle_up(f"pager/{self.device_id}/up", payload)

    def ack(self, relay: Relay, key: bytes, msg_id: str) -> None:
        self.up(relay, key, {"id": msg_id, "ack": "shown"})

    def text(self, relay: Relay, key: bytes, msg_id: str, body: str = "hi") -> None:
        self.up(relay, key, {"id": msg_id, "from": "kid", "to": "mom", "body": body, "ack": None})


def cfg_mv(envs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The `cfg.mv` maps among `envs`."""
    return [e["cfg"]["mv"] for e in envs if e.get("kind") == "cfg" and "mv" in e.get("cfg", {})]


def pages(envs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The text pages among `envs` (no `kind`), not book, cfg or receipts."""
    return [e for e in envs if e.get("kind") in (None, "msg")]
