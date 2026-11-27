"""A whole move between two relays in one process (docs/MOVE_DESIGN.md §4): freeze on the source, bundle,
import on the target, cutover, a pager that follows the order, a page sent before the freeze, a text the
frozen source refused, and a bridge that follows `moved`."""

from __future__ import annotations

import hashlib
import json

from app import movekeys
from app.db.firestore import get_db
from app.move import hello_sig
from app.store import devices as devices_store
from app.store import messages as messages_store
from tests.fed_world import needs_two_relays
from tests.move_world import (
    DST_URL,
    KEY,
    SimPager,
    cfg_mv,
    make_world,
    pages,
    published,
)

pytestmark = needs_two_relays


def _sent(world, relay, body: str):
    with relay.active():
        res = relay.app.state.routing.send(
            sender_uid="mom",
            recipient_alias="kid",
            kind="text",
            body=body,
            origin_backend_kind="webapp",
        )
    return res.messages[0]


def _pager_delivery(msg_id: str) -> messages_store.Delivery:
    msg = messages_store.get_message(msg_id)
    assert msg is not None
    return next(d for d in msg.deliveries.values() if d.kind == "pager")


def _heartbeat(relay, token: str):
    return relay.raw(
        "POST",
        "/bridge/heartbeat",
        content=b"{}",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )


def test_whole_move(net, make_relay):
    world = make_world(net, make_relay)
    src, dst = world.src, world.dst
    pager = SimPager("pgr-a")

    # before the freeze: the pager is online on the source, and a page goes out that it never shows
    pager.status(src, KEY)
    before = _sent(world, src, "written before the freeze")
    assert [e["id"] for e in pages(published(src, "pgr-a", KEY))] == [before.id]

    move_id = world.freeze()
    assert dst.raw("GET", "/api/public/move-info").json()["move"] is None

    # the frozen source refuses a text with the moving receipt and stores nothing
    src.broker.clear()
    pager.text(src, KEY, "u_91c0aa11", "typed while frozen")
    receipts = [e for e in published(src, "pgr-a", KEY) if e.get("kind") == "rcpt"]
    assert [(r["ref"], r["st"], r["rsn"]) for r in receipts] == [("u_91c0aa11", "failed", "moving")]
    with src.active():
        assert not messages_store.wire_id_exists("u_91c0aa11", "mom")

    # the target is empty and, with nobody signed up, the bundle and passphrase authorise the import
    data = world.bundle()
    dry = world.import_bundle(data, dry=True)
    assert dry.status_code == 200 and dry.json()["ok"] is True, dry.text
    real = world.import_bundle(data)
    assert real.status_code == 201, real.text
    info = dst.raw("GET", "/api/public/move-info").json()
    assert info["move"] == {"id": move_id, "imported": True} and info["empty"] is False
    dst.sign_in("sup", "mom", "kid")

    # imported, waiting: a page written on the target stays queued and nothing is published
    page = _sent(world, dst, "written on the target")
    with dst.active():
        delivery = _pager_delivery(page.id)
    assert (delivery.state, delivery.attempts) == ("queued", 0)
    assert dst.broker.published == []
    dev = world.target_state("pgr-a")
    assert (dev.provisionState, dev.move.state) == ("moving", "waiting")

    # cutover: the order goes out under k and the pager acks it on the source
    assert world.cutover() == 1
    order = cfg_mv(published(src, "pgr-a", KEY))[-1]
    assert order["id"] == move_id and order["host"] == "new.example.org" and order["port"] == 8883
    order_env = [e for e in published(src, "pgr-a", KEY) if e.get("kind") == "cfg"][-1]
    pager.ack(src, KEY, order_env["id"])
    assert world.src.get("/api/admin/move", "sup").json()["devices"][0]["ackAt"] is not None

    # the pager derives k' and pw' and trials on the target
    pager.derive(move_id)
    k2, pw2 = movekeys.derive(KEY, move_id, "pgr-a")
    assert (pager.k_prime, pager.pw_prime) == (k2, pw2)
    n = pager.status(dst, k2, mv={"id": move_id, "st": "try", "at": 1}, cbor=True)
    sent = published(dst, "pgr-a", k2)
    proof = cfg_mv(sent)
    assert proof == [{"id": move_id, "ok": n}]
    # what it was owed arrives once each, under the original ids
    ids = [e["id"] for e in pages(sent)]
    assert sorted(ids) == sorted([before.id, page.id])
    assert world.target_state("pgr-a").move.state == "trying"

    # the proof's ack commits
    proof_env = next(e for e in sent if e.get("kind") == "cfg" and "mv" in e["cfg"])
    pager.ack(dst, k2, proof_env["id"])
    dev = world.target_state("pgr-a")
    assert (dev.provisionState, dev.move.state) == ("provisioned", "moved")
    for mid in (before.id, page.id):
        pager.ack(dst, k2, mid)
    with dst.active():
        assert {_pager_delivery(mid).state for mid in (before.id, page.id)} == {"shown"}

    # the text the source refused is sent again, to the target, and stored once
    pager.text(dst, k2, "u_91c0aa11", "typed while frozen")
    pager.text(dst, k2, "u_91c0aa11", "typed while frozen")
    with dst.active():
        assert messages_store.wire_id_exists("u_91c0aa11", "mom")
        bodies = [m.body for m in _all_messages()]
    assert bodies.count("typed while frozen") == 1

    # the bridge: the source answers `moved`; the target proves itself with `hello`, then serves it
    token = world.bridge_token
    answer = _heartbeat(src, token)
    assert answer.status_code == 200
    moved = answer.json()["moved"]
    assert moved == {"url": DST_URL, "id": move_id}
    bridge_id = world.bridge_id
    nonce = "00112233445566778899aabbccddeeff"
    hello = dst.raw("GET", f"/bridge/hello?bridge={bridge_id}&nonce={nonce}")
    assert hello.status_code == 200
    want = hello_sig(hashlib.sha256(token.encode()).hexdigest(), nonce, bridge_id, move_id)
    assert hello.json() == {"bridgeId": bridge_id, "moveId": move_id, "sig": want}
    on_target = _heartbeat(dst, token)
    assert on_target.status_code == 200 and "moved" not in on_target.json()
    page_rows = dst.get("/api/admin/move", "sup").json()
    assert page_rows["bridges"][0]["arrived"] is True
    assert page_rows["devices"][0]["state"] == "moved"
    assert json.dumps(page_rows)  # serialisable


def _all_messages() -> list[messages_store.Message]:
    return [
        messages_store.Message.model_validate({"id": s.id, **(s.to_dict() or {})})
        for s in get_db().collection("messages").stream()
    ]


def test_a_pager_that_never_arrives_gets_a_setup_code(net, make_relay):
    world = make_world(net, make_relay)
    dst = world.dst
    world.moved_over()
    rows = dst.get("/api/admin/move", "sup").json()["devices"]
    assert [(r["deviceId"], r["state"], r["remedy"]) for r in rows] == [
        ("pgr-a", "waiting", "switch_now_or_setup")
    ]
    r = dst.post("/api/admin/devices/pgr-a/rotate-credentials", "sup")
    assert r.status_code == 200, r.text
    dev = world.target_state("pgr-a")
    assert (dev.provisionState, dev.move.state) == ("issued", "reprovisioned")
    rows = dst.get("/api/admin/move", "sup").json()["devices"]
    assert rows[0]["state"] == "reprovisioned" and rows[0]["remedy"] == "none"


def test_an_old_session_cannot_sign_a_status_under_the_old_key(net, make_relay):
    world = make_world(net, make_relay)
    world.moved_over()
    pager = SimPager("pgr-a")
    pager.status(world.dst, KEY, mv={"id": "mv_whatever", "st": "try", "at": 1})
    dev = world.target_state("pgr-a")
    assert dev.move.state == "waiting" and dev.move.arrivedAt is None
    assert isinstance(dev, devices_store.Device)
