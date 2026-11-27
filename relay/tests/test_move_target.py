"""The target side of the over-the-air move (docs/MOVE_DESIGN.md §4, §5.4, §7, §9, §10, §12): `move-info`,
who may import, the device lifecycle (waiting, trying, moved), the setup-code remedy, the target
page and its actions, `mustChange` and claim codes, and the OTA rule."""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from app import authn_home, devcfg, firmware, move_info, movekeys, soracom
from app import move as move_module
from app.db.firestore import get_db
from app.store import device_secrets as device_secrets_store
from app.store import devices as devices_store
from app.store import messages as messages_store
from tests.fed_world import needs_two_relays
from tests.move_world import (
    DST_URL,
    KEY,
    PASS,
    SimPager,
    cfg_mv,
    make_world,
    pages,
    published,
)
from tests.test_firmware import BASE_URL, ID_NEW, ID_OLD, INDEX_URL, index_server  # noqa: F401

pytestmark = needs_two_relays

CSRF = {"X-Requested-With": "pager"}
LONG_AGO = datetime(2023, 11, 14, tzinfo=UTC)


@pytest.fixture
def world(net, make_relay):
    return make_world(net, make_relay)


@pytest.fixture
def live(world):
    """Frozen, bundled, imported; sessions for the imported people."""
    world.move_id = world.moved_over()
    return world


def _arrive(
    world, *, mv_st: str = "try", cbor: bool = False, **status: Any
) -> tuple[SimPager, int]:
    pager = SimPager("pgr-a")
    pager.derive(world.move_id)
    n = pager.status(
        world.dst,
        pager.k_prime,
        mv={"id": world.move_id, "st": mv_st, "at": 1},
        cbor=cbor,
        **status,
    )
    return pager, n


def _page(relay, body: str):
    with relay.active():
        return (
            relay.app.state.routing.send(
                sender_uid="mom",
                recipient_alias="kid",
                kind="text",
                body=body,
                origin_backend_kind="webapp",
            )
            .messages[0]
        )


def _device(world, device_id: str = "pgr-a") -> devices_store.Device:
    return world.target_state(device_id)


# ---- move-info ---------------------------------------------------------------------------------


def test_move_info_describes_the_target(net, make_relay):
    world = make_world(net, make_relay, beam_port=8884)
    r = world.dst.raw("GET", "/api/public/move-info")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    info = move_info.MoveInfo.model_validate(r.json())
    assert info.authBackend == "home" and info.baseUrl == DST_URL
    assert (info.broker.host, info.broker.port) == ("new.example.org", 8883)
    assert (info.beam.host, info.beam.port) == ("new.example.org", 8884)
    assert info.ca is not None and info.ca.url == f"{DST_URL}/ca/{info.ca.sha}.pem"
    assert info.empty is True and info.move is None
    assert info.schemaVersion == 2
    # the pointer resolves on the same server
    pem = world.dst.raw("GET", f"/ca/{info.ca.sha}.pem")
    assert pem.status_code == 200 and pem.text.startswith("-----BEGIN")


def test_move_info_has_no_beam_without_a_port_and_no_ca_without_one(net, make_relay):
    relay = make_relay("bare", host="bare.example.org", broker_host="bare.example.org")
    body = relay.raw("GET", "/api/public/move-info").json()
    assert body["beam"] is None and body["ca"] is None


def test_move_info_needs_no_sign_in_and_reports_the_import(live):
    anon = live.dst.raw("GET", "/api/public/move-info")
    assert anon.status_code == 200
    assert anon.json()["move"] == {"id": live.move_id, "imported": True}
    assert anon.json()["empty"] is False


def test_move_info_is_not_empty_while_it_holds_a_family(world):
    assert world.src.raw("GET", "/api/public/move-info").json()["empty"] is False


def test_move_info_refuses_without_a_public_base_url(make_relay):
    relay = make_relay("nourl")
    relay.app.state.settings = dataclasses.replace(relay.settings, public_base_url=None)
    r = relay.raw("GET", "/api/public/move-info")
    assert r.status_code == 503


# ---- who may import ----------------------------------------------------------------------------


def _import_as(relay, uid: str, data: bytes, *, dry: bool = False):
    headers = {**relay.auth(uid), "X-Export-Passphrase": PASS}
    return relay.raw(
        "POST", "/api/admin/move/import" + ("?dryRun=1" if dry else ""), content=data, headers=headers
    )


def test_an_empty_relay_with_no_users_takes_the_import_on_the_bundles_passphrase(world):
    world.freeze()
    data = world.bundle()
    wrong = world.dst.raw(
        "POST", "/api/admin/move/import", content=data, headers={"X-Export-Passphrase": "nope-nope-nope"}
    )
    assert wrong.status_code == 400
    assert world.import_bundle(data).status_code == 201


def test_the_unauthenticated_import_is_rate_limited(world):
    codes = [
        world.dst.raw("POST", "/api/admin/move/import?dryRun=1", content=b"x").status_code
        for _ in range(11)
    ]
    assert codes[:10] == [400] * 10 and codes[10] == 429


def test_once_there_are_people_only_a_super_may_import(world):
    world.freeze()
    data = world.bundle()
    dst = world.dst
    dst.family("other", "Other", federated=False)
    dst.user("boss", "boss", "super", "other")
    dst.user("kidz", "kidz", "member", "other")
    assert world.import_bundle(data, dry=True).status_code == 401
    r = _import_as(dst, "kidz", data, dry=True)
    assert r.status_code == 403
    # a family is held, so even a super's dry run reports it
    r = _import_as(dst, "boss", data, dry=True)
    assert r.status_code == 200 and r.json()["ok"] is False
    assert any("empty server" in e for e in r.json()["errors"])
    r = _import_as(dst, "boss", data)
    assert r.status_code == 409 and r.json()["ok"] is False and "detail" in r.json()


def test_the_first_run_admin_is_replaced_by_the_import(world):
    """The first-run page made an admin and the default family; the import takes their place."""
    world.freeze()
    data = world.bundle()
    dst = world.dst
    with dst.active():
        made = dst.http.post(
            "/api/setup/first-admin", json={"username": "admin", "password": "first-run-pass"}, headers=CSRF
        )
        assert made.status_code == 200, made.text
        token = made.cookies.get("pager_session")
        dst.http.cookies.clear()
        assert dst.raw("GET", "/api/public/move-info").json()["empty"] is True
    headers = {"Authorization": f"Bearer {token}", "X-Export-Passphrase": PASS}
    anon = world.import_bundle(data, dry=True)
    assert anon.status_code == 401
    dry = dst.raw("POST", "/api/admin/move/import?dryRun=1", content=data, headers=headers)
    assert dry.status_code == 200 and dry.json()["ok"] is True, dry.text
    real = dst.raw("POST", "/api/admin/move/import", content=data, headers=headers)
    assert real.status_code == 201, real.text
    with dst.active():
        # the source's own admin (same alias as the placeholder) is the one that remains
        from app.store import users as users_store

        assert users_store.get_user("sup") is not None
        assert users_store.get_user_by_alias("admin").uid == "sup"
        assert authn_home.session_uid(token) is None


def test_a_first_run_relay_that_has_been_used_is_not_replaced(world):
    world.freeze()
    data = world.bundle()
    dst = world.dst
    with dst.active():
        made = dst.http.post(
            "/api/setup/first-admin", json={"username": "boss", "password": "first-run-pass"}, headers=CSRF
        )
        token = made.cookies.get("pager_session")
        dst.http.cookies.clear()
    with dst.active():
        devices_store.create_device(
            device_id="pgr-here", owner_uid="x", label="l", mqtt_username="pgr-here",
            mqtt_password_hash="h", family_id="default",
        )
    headers = {"Authorization": f"Bearer {token}", "X-Export-Passphrase": PASS}
    r = dst.raw("POST", "/api/admin/move/import?dryRun=1", content=data, headers=headers)
    assert r.status_code == 200 and r.json()["ok"] is False


def test_a_bundle_for_another_address_is_refused(world, make_relay):
    world.freeze()
    data = world.bundle()
    other = make_relay("elsewhere", host="else.example.org", broker_host="else.example.org")
    r = other.raw(
        "POST", "/api/admin/move/import", content=data, headers={"X-Export-Passphrase": PASS}
    )
    assert r.status_code == 409
    assert any("not to this server" in e for e in r.json()["errors"])


def test_a_second_import_is_refused(live):
    r = live.import_bundle(live.bundle(), headers={"Authorization": f"Bearer {live.dst.tokens['sup']}"})
    assert r.status_code == 409 and "already" in r.json()["detail"]


def test_an_import_creates_the_derived_logins(live):
    k2, pw2 = movekeys.derive(KEY, live.move_id, "pgr-a")
    assert ("pgr-a", pw2, "pgr-a") in live.dst.emqx.ensured_devices
    with live.dst.active():
        secret = device_secrets_store.get("pgr-a")
    assert secret is not None and secret.hmacKey == k2


# ---- the device lifecycle ----------------------------------------------------------------------


def test_waiting_to_trying_to_moved_with_the_proof_carrying_the_statuss_n(live):
    assert (_device(live).provisionState, _device(live).move.state) == ("moving", "waiting")
    pager, n = _arrive(live, cbor=True)
    dev = _device(live)
    assert dev.move.state == "trying" and dev.move.trialAt is not None and dev.provisionState == "moving"
    proofs = [e for e in published(live.dst, "pgr-a", pager.k_prime) if e.get("kind") == "cfg"]
    assert cfg_mv(proofs) == [{"id": live.move_id, "ok": n}]
    pager.ack(live.dst, pager.k_prime, proofs[-1]["id"])
    dev = _device(live)
    assert (dev.move.state, dev.provisionState) == ("moved", "provisioned")
    assert dev.move.movedAt is not None


def test_each_try_gets_its_own_proof_with_that_statuss_n(live):
    pager, n1 = _arrive(live)
    n2 = pager.status(
        live.dst, pager.k_prime, mv={"id": live.move_id, "st": "try", "at": 2}, session="s_00000002"
    )
    assert n2 > n1
    proofs = cfg_mv([e for e in published(live.dst, "pgr-a", pager.k_prime) if e.get("kind") == "cfg"])
    assert proofs[-1]["ok"] == n2 and n1 in [p["ok"] for p in proofs]


def test_a_report_of_ok_is_an_arrival_and_commits(live):
    pager, _n = _arrive(live, mv_st="ok")
    dev = _device(live)
    assert (dev.move.state, dev.provisionState) == ("moved", "provisioned")
    assert cfg_mv(published(live.dst, "pgr-a", pager.k_prime)) == []


def test_a_report_for_another_move_changes_nothing(live):
    pager = SimPager("pgr-a")
    pager.derive(live.move_id)
    pager.status(live.dst, pager.k_prime, mv={"id": "mv_00000000other", "st": "ok", "at": 1})
    dev = _device(live)
    assert dev.move.state == "waiting" and dev.move.arrivedAt is not None
    assert cfg_mv(published(live.dst, "pgr-a", pager.k_prime)) == []


def test_a_status_signed_with_the_old_key_is_not_an_arrival(live):
    SimPager("pgr-a").status(live.dst, KEY, mv={"id": live.move_id, "st": "ok", "at": 1})
    dev = _device(live)
    assert dev.move.state == "waiting" and dev.move.arrivedAt is None


def test_an_ack_of_something_else_does_not_commit(live):
    pager, _n = _arrive(live)
    pager.ack(live.dst, pager.k_prime, "m_notthisone")
    assert _device(live).move.state == "trying"


def test_the_source_order_left_in_the_bundle_is_not_resent_by_the_target(world):
    move_id = world.freeze()
    with world.src.active():  # a cancel sits in the slot when the bundle is taken
        devcfg.cancel_move("pgr-a", move_id, world.src.broker)
    data = world.bundle()
    assert world.import_bundle(data).status_code == 201
    with world.dst.active():
        assert get_db().collection("devices").document("pgr-a").get().to_dict().get("pendingCfgMv") is None
    world.move_id = move_id
    pager, _n = _arrive(world, mv_st="pend")
    assert cfg_mv(published(world.dst, "pgr-a", pager.k_prime)) == []
    with world.dst.active():
        assert get_db().collection("devices").document("pgr-a").get().to_dict().get("pendingCfgMv") is None


def test_a_waiting_pager_costs_no_attempts_and_gets_its_pages_on_arrival_with_their_ids(live):
    pages_sent = [_page(live.dst, f"page {i}") for i in range(3)]
    with live.dst.active():
        for m in pages_sent:
            d = next(x for x in messages_store.get_message(m.id).deliveries.values() if x.kind == "pager")
            assert (d.state, d.attempts) == ("queued", 0)
    # the tick and a repeat do not change it
    for m in pages_sent:
        with live.dst.active():
            live.dst.app.state.routing.redeliver_pager(messages_store.get_message(m.id), "pgr-a")
    assert live.dst.broker.published == []
    with live.dst.active():
        assert all(
            next(x for x in messages_store.get_message(m.id).deliveries.values() if x.kind == "pager").attempts == 0
            for m in pages_sent
        )
    pager, _n = _arrive(live)
    got = [e["id"] for e in pages(published(live.dst, "pgr-a", pager.k_prime))]
    assert got == [m.id for m in pages_sent]
    with live.dst.active():
        for m in pages_sent:
            d = next(x for x in messages_store.get_message(m.id).deliveries.values() if x.kind == "pager")
            assert d.state == "sent"


def test_the_first_signed_status_ends_the_hold_even_without_a_move_report(live):
    page = _page(live.dst, "hello")
    pager, _n = _arrive(live, mv_st="pend")
    assert [e["id"] for e in pages(published(live.dst, "pgr-a", pager.k_prime))] == [page.id]
    assert _device(live).move.state == "waiting"
    later = _page(live.dst, "later")
    assert later.id in [e["id"] for e in pages(published(live.dst, "pgr-a", pager.k_prime))]


@pytest.mark.parametrize("stage", ["waiting", "trying", "moved"])
def test_a_setup_code_ends_the_move_in_every_state(live, stage):
    if stage != "waiting":
        _arrive(live, mv_st="try" if stage == "trying" else "ok")
        if stage == "trying":
            assert _device(live).move.state == "trying"
    with live.dst.active():
        before = device_secrets_store.get("pgr-a")
    r = live.dst.post("/api/admin/devices/pgr-a/rotate-credentials", "sup")
    assert r.status_code == 200, r.text
    dev = _device(live)
    assert dev.move.state == "reprovisioned" and dev.move.reprovisionedAt is not None
    assert dev.provisionState == "issued"
    with live.dst.active():
        after = device_secrets_store.get("pgr-a")
        slot = get_db().collection("devices").document("pgr-a").get().to_dict().get("pendingCfgMv")
    assert after.hmacKey != before.hmacKey and slot is None
    # the login the setup code made is not the derived one
    k2, pw2 = movekeys.derive(KEY, live.move_id, "pgr-a")
    assert live.dst.emqx.ensured_devices[-1][1] != pw2
    # the old derived key no longer verifies
    SimPager("pgr-a").status(live.dst, k2)
    assert _device(live).provisionState == "issued"


def test_a_setup_code_for_a_pager_with_no_credentials_creates_them(world):
    world.device("pgr-b", "kid", key=bytes(range(50, 82)))
    with world.src.active():
        get_db().collection("devices").document("pgr-b").update({"authMode": "password"})
    world.freeze()
    assert world.import_bundle(world.bundle()).status_code == 201
    world.dst.sign_in("sup")
    dev = _device(world, "pgr-b")
    assert dev.provisionState == "moved"
    row = next(r for r in world.dst.get("/api/admin/move", "sup").json()["devices"] if r["deviceId"] == "pgr-b")
    assert (row["state"], row["remedy"]) == ("waiting", "setup_code")
    r = world.dst.post("/api/admin/devices/pgr-b/rotate-credentials", "sup")
    assert r.status_code == 200, r.text
    with world.dst.active():
        assert device_secrets_store.get("pgr-b") is not None
    assert _device(world, "pgr-b").provisionState == "issued"


# ---- the target page ---------------------------------------------------------------------------


def test_the_target_page_reads_the_whole_move(live):
    dst = live.dst
    body = dst.get("/api/admin/move", "sup").json()
    assert body["role"] == "target" and body["id"] == live.move_id and body["state"] == "imported"
    assert body["sourceBaseUrl"] == "https://old.example.org"
    assert [(r["deviceId"], r["state"], r["remedy"]) for r in body["devices"]] == [
        ("pgr-a", "waiting", "switch_now_or_setup")
    ]
    assert body["devices"][0]["owner"] == "kid" and body["devices"][0]["bearer"] == "direct"
    assert {p["alias"] for p in body["people"]} == {"admin", "kid", "mom"}
    assert all(p["signedIn"] for p in body["people"])  # `moved_over` signed them in
    assert [b["arrived"] for b in body["bridges"]] == [False]
    assert body["fedLinks"] == [] and body["beam"] is None and body["bridgeRedirect"] is None


def test_the_target_page_marks_a_dark_pager(live):
    with live.dst.active():
        get_db().collection("devices").document("pgr-a").update({"status.updatedAt": LONG_AGO})
    row = live.dst.get("/api/admin/move", "sup").json()["devices"][0]
    assert row["darkSince"] is not None and row["lastSeenAt"] is not None


def test_a_bridge_arrives_when_it_asks_here(live):
    token = live.bridge_token
    r = live.dst.raw(
        "POST", "/bridge/heartbeat", content=b"{}",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    assert r.status_code == 200
    assert live.dst.get("/api/admin/move", "sup").json()["bridges"][0]["arrived"] is True


def test_the_target_routes_are_super_only(live):
    dst = live.dst
    for method, path in (
        ("GET", "/api/admin/move"),
        ("POST", "/api/admin/move/beam/switch"),
        ("POST", "/api/admin/move/beam/switch-back"),
        ("POST", "/api/admin/move/bridges/send-back"),
        ("POST", "/api/admin/move/end"),
    ):
        assert dst.raw(method, path).status_code == 401, path
        assert dst.api(method, path, "kid").status_code == 403, path


def test_send_bridges_back_points_the_phones_at_the_source(live):
    token = live.bridge_token
    r = live.dst.post("/api/admin/move/bridges/send-back", "sup", {})
    assert r.status_code == 200 and r.json() == {"bridgeRedirect": "https://old.example.org"}
    beat = live.dst.raw(
        "POST", "/bridge/heartbeat", content=b"{}",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    assert beat.json()["moved"] == {"url": "https://old.example.org", "id": live.move_id}
    page = live.dst.get("/api/admin/move", "sup").json()
    assert page["bridgeRedirect"] == "https://old.example.org"
    # it keeps answering `hello` with the same id
    hello = live.dst.raw("GET", f"/bridge/hello?bridge={live.bridge_id}&nonce={'a' * 32}")
    assert hello.status_code == 200 and hello.json()["moveId"] == live.move_id


def test_a_bridge_that_stops_asking_is_no_longer_there_after_send_back(live):
    live.dst.raw(
        "POST", "/bridge/heartbeat", content=b"{}",
        headers={"Authorization": f"Bearer {live.bridge_token}", "Content-Type": "application/json"},
    )
    live.dst.post("/api/admin/move/bridges/send-back", "sup", {})
    assert live.dst.get("/api/admin/move", "sup").json()["bridges"][0]["arrived"] is True
    with live.dst.active():
        get_db().collection("bridges").document(live.bridge_id).update({"lastSeenAt": LONG_AGO})
    assert live.dst.get("/api/admin/move", "sup").json()["bridges"][0]["arrived"] is False


def test_end_archives_the_record_and_keeps_the_logins(live):
    r = live.dst.post("/api/admin/move/end", "sup", {})
    assert r.status_code == 200
    assert live.dst.get("/api/admin/move", "sup").json()["state"] == "ended"
    assert live.dst.post("/api/admin/move/end", "sup", {}).status_code == 409
    assert live.dst.post("/api/admin/move/bridges/send-back", "sup", {}).status_code == 409
    pager, _n = _arrive(live, mv_st="ok")  # the pager can still arrive
    assert _device(live).move.state == "moved"
    assert pager.k_prime is not None


def test_the_actions_need_an_imported_move(world):
    world.dst.family("f", "F", federated=False)
    world.dst.user("boss", "boss", "super", "f")
    for path in ("beam/switch", "beam/switch-back", "bridges/send-back", "end"):
        assert world.dst.post(f"/api/admin/move/{path}", "boss", {}).status_code == 409, path
    assert world.dst.get("/api/admin/move", "boss").json() == {"role": "idle", "state": "none"}


# ---- Beam --------------------------------------------------------------------------------------


class FakeSoracom:
    """Two groups' worth of SIMs, the calls that matter and a way to fail one SIM."""

    def __init__(self) -> None:
        self.groups = {"g-old": "pager-beam"}
        self.destinations: dict[str, str] = {}
        self.sims = {"i1": "g-old", "i2": "g-old", "i3": "g-other"}
        self.fail_on: set[str] = set()
        self.calls: list[tuple[str, str]] = []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        path = req.url.path.removeprefix("/v1")
        self.calls.append((req.method, path))
        if path == "/auth":
            return httpx.Response(200, json={"apiKey": "AK", "token": "TK"})
        if (req.method, path) == ("GET", "/groups"):
            name = req.url.params.get("tag_value")
            return httpx.Response(
                200, json=[{"groupId": g, "tags": {"name": n}} for g, n in self.groups.items() if n == name]
            )
        if (req.method, path) == ("POST", "/groups"):
            name = json.loads(req.content)["tags"]["name"]
            self.groups["g-new"] = name
            return httpx.Response(201, json={"groupId": "g-new"})
        if path.startswith("/groups/") and req.method == "PUT":
            self.destinations[path.split("/")[2]] = json.loads(req.content)[0]["value"]["destination"]
            return httpx.Response(200, json={})
        if (req.method, path) == ("GET", "/subscribers"):
            return httpx.Response(
                200, json=[{"imsi": i, "groupId": g, "status": "active"} for i, g in self.sims.items()]
            )
        if path.endswith("/set_group"):
            imsi = path.split("/")[2]
            if imsi in self.fail_on:
                return httpx.Response(500, text="no")
            self.sims[imsi] = json.loads(req.content)["groupId"]
            return httpx.Response(200, json={})
        raise AssertionError(f"unexpected {req.method} {path}")


@pytest.fixture
def beam(net, make_relay) -> Iterator[Any]:
    world = make_world(
        net, make_relay, beam_port=8884, soracom_auth_key_id="kid-x", soracom_auth_key="key-x"
    )
    world.move_id = world.moved_over()
    world.soracom = FakeSoracom()
    soracom._transport = httpx.MockTransport(world.soracom)
    yield world
    soracom._transport = None


def _beam_pager(world, state: str = "waiting") -> None:
    with world.dst.active():
        devices_store.update_status("pgr-a", tls="proxy", state="online")


def test_the_beam_block_counts_beam_pagers(beam):
    _beam_pager(beam)
    block = beam.dst.get("/api/admin/move", "sup").json()["beam"]
    assert block == {
        "host": "new.example.org", "port": 8884, "count": 1, "movedCount": 0,
        "switched": False, "soracom": True, "byHand": False,
    }


def test_switch_moves_the_old_groups_sims_into_a_new_group(beam):
    r = beam.dst.post("/api/admin/move/beam/switch", "sup", {"byHand": False})
    assert r.status_code == 200 and r.json() == {"switched": True, "byHand": False, "moved": 2}
    fake = beam.soracom
    assert fake.groups["g-new"] == "pager-beam-new.example.org"
    assert fake.destinations["g-new"] == "mqtts://new.example.org:8884"
    assert fake.sims == {"i1": "g-new", "i2": "g-new", "i3": "g-other"}
    assert beam.dst.get("/api/admin/move", "sup").json()["beam"]["switched"] is True
    assert beam.dst.post("/api/admin/move/beam/switch", "sup", {}).status_code == 409


def test_switch_back_while_no_beam_pager_has_moved(beam):
    beam.dst.post("/api/admin/move/beam/switch", "sup", {})
    assert beam.soracom.sims["i1"] == "g-new"
    r = beam.dst.post("/api/admin/move/beam/switch-back", "sup", {})
    assert r.status_code == 200 and r.json()["switched"] is False
    assert beam.soracom.sims == {"i1": "g-old", "i2": "g-old", "i3": "g-other"}
    assert beam.dst.post("/api/admin/move/beam/switch-back", "sup", {}).status_code == 409


def test_switch_back_is_refused_once_a_beam_pager_has_moved(beam):
    _beam_pager(beam)
    beam.dst.post("/api/admin/move/beam/switch", "sup", {})
    _arrive(beam, mv_st="ok", tls="proxy")
    assert _device(beam).move.state == "moved"
    block = beam.dst.get("/api/admin/move", "sup").json()["beam"]
    assert block["movedCount"] == 1
    r = beam.dst.post("/api/admin/move/beam/switch-back", "sup", {})
    assert r.status_code == 409
    assert beam.soracom.sims["i1"] == "g-new"


def test_a_non_beam_pager_moving_does_not_block_switch_back(beam):
    beam.dst.post("/api/admin/move/beam/switch", "sup", {})
    _arrive(beam, mv_st="ok")  # direct bearer
    assert beam.dst.post("/api/admin/move/beam/switch-back", "sup", {}).status_code == 200


def test_a_failed_enrol_puts_the_sims_back(beam):
    beam.soracom.fail_on = {"i2"}
    r = beam.dst.post("/api/admin/move/beam/switch", "sup", {})
    assert r.status_code == 502
    assert beam.soracom.sims["i1"] == "g-old"
    assert beam.dst.get("/api/admin/move", "sup").json()["beam"]["switched"] is False


def test_the_old_group_must_exist(beam):
    beam.soracom.groups = {}
    r = beam.dst.post("/api/admin/move/beam/switch", "sup", {})
    assert r.status_code == 409 and "pager-beam" in r.json()["detail"]


def test_by_hand_is_recorded_and_switch_back_is_by_hand_too(beam):
    r = beam.dst.post("/api/admin/move/beam/switch", "sup", {"byHand": True})
    assert r.json() == {"switched": True, "byHand": True, "moved": 0}
    assert beam.soracom.calls == []
    block = beam.dst.get("/api/admin/move", "sup").json()["beam"]
    assert block["switched"] is True and block["byHand"] is True
    r = beam.dst.post("/api/admin/move/beam/switch-back", "sup", {})
    assert r.status_code == 200 and r.json()["byHand"] is True and beam.soracom.calls == []


def test_without_a_soracom_key_the_switch_is_by_hand_only(net, make_relay):
    world = make_world(net, make_relay, beam_port=8884)
    world.move_id = world.moved_over()
    block = world.dst.get("/api/admin/move", "sup").json()["beam"]
    assert block["soracom"] is False
    r = world.dst.post("/api/admin/move/beam/switch", "sup", {"byHand": False})
    assert r.status_code == 409 and "by hand" in r.json()["detail"]
    assert world.dst.post("/api/admin/move/beam/switch", "sup", {"byHand": True}).status_code == 200


def test_there_is_no_switch_without_a_beam_listener(live):
    assert live.dst.post("/api/admin/move/beam/switch", "sup", {"byHand": True}).status_code == 409


# ---- accounts ----------------------------------------------------------------------------------


def _login(relay, alias: str, password: str):
    with relay.active():
        return relay.http.post("/api/auth/login", json={"username": alias, "password": password}, headers=CSRF)


def test_an_admin_set_password_must_be_changed_first(net, make_relay):
    world = make_world(net, make_relay)
    world.move_id = world.freeze(accounts=[{"uid": "mom", "password": "temporary-pass-1"}])
    assert world.import_bundle(world.bundle()).status_code == 201
    dst = world.dst
    r = _login(dst, "mom", "temporary-pass-1")
    assert r.status_code == 200, r.text
    token = r.cookies.get("pager_session")
    auth = {"Authorization": f"Bearer {token}"}
    with dst.active():
        session = dst.http.get("/api/auth/session", headers=auth)
        assert session.json()["mustChange"] is True
        # nothing else works
        for path in ("/api/me", "/api/me/move", "/api/admin/move"):
            resp = dst.http.get(path, headers=auth)
            assert resp.status_code == 403 and resp.json()["detail"] == "password change required", path
        wrong = dst.http.post(
            "/api/auth/password", headers={**auth, **CSRF},
            json={"currentPassword": "not-it-at-all", "newPassword": "a-new-password-1"},
        )
        assert wrong.status_code == 403
        same = dst.http.post(
            "/api/auth/password", headers={**auth, **CSRF},
            json={"currentPassword": "temporary-pass-1", "newPassword": "temporary-pass-1"},
        )
        assert same.status_code == 422
        short = dst.http.post(
            "/api/auth/password", headers={**auth, **CSRF},
            json={"currentPassword": "temporary-pass-1", "newPassword": "short"},
        )
        assert short.status_code == 422
        ok = dst.http.post(
            "/api/auth/password", headers={**auth, **CSRF},
            json={"currentPassword": "temporary-pass-1", "newPassword": "a-new-password-1"},
        )
        assert ok.status_code == 200, ok.text
        assert dst.http.get("/api/auth/session", headers=auth).json()["mustChange"] is False
        assert dst.http.get("/api/me", headers=auth).status_code == 200
    assert _login(dst, "mom", "temporary-pass-1").status_code == 401
    assert _login(dst, "mom", "a-new-password-1").status_code == 200


def test_claim_codes_sign_in_and_are_spent_once_the_person_has_signed_in(net, make_relay):
    world = make_world(net, make_relay)
    world.freeze()
    claim = world.src.get("/api/me/move", "kid").json()["claim"]["url"]
    code = claim.split("#invite=")[1]
    assert world.import_bundle(world.bundle()).status_code == 201
    dst = world.dst
    with dst.active():
        first = dst.http.post("/api/auth/invite/redeem", json={"code": code, "password": "my-own-password"}, headers=CSRF)
    assert first.status_code == 200 and first.json()["alias"] == "kid"
    with dst.active():
        again = dst.http.post("/api/auth/invite/redeem", json={"code": code}, headers=CSRF)
    assert again.status_code == 401
    # the second try says why only for a person who signed in another way
    other = world.src.get("/api/me/move", "mom").json()["claim"]["url"].split("#invite=")[1]
    with dst.active():
        authn_home.set_password("mom", "mom-picked-this")
    assert _login(dst, "mom", "mom-picked-this").status_code == 200
    with dst.active():
        spent = dst.http.post("/api/auth/invite/redeem", json={"code": other}, headers=CSRF)
    assert spent.status_code == 401 and "already used" in spent.json()["detail"]
    assert not any(
        (s.to_dict() or {}).get("createdBy") == "move" and not (s.to_dict() or {}).get("spent")
        for s in _invites(dst, "mom")
    )


def _invites(relay, uid: str):
    with relay.active():
        return [s for s in get_db().collection("inviteCodes").stream() if (s.to_dict() or {}).get("uid") == uid]


# ---- OTA ---------------------------------------------------------------------------------------


@pytest.fixture
def ota(net, make_relay, index_server):  # noqa: F811
    world = make_world(net, make_relay, fw_index_url=INDEX_URL, fw_bucket_base=BASE_URL)
    world.move_id = world.moved_over()
    with world.dst.active():
        devices_store.update_status("pgr-a", state="online", otaCap=1, mvc=1, img=ID_OLD[:16])
    world.index = index_server
    return world


def _push(world, target: str):
    return world.dst.post("/api/admin/devices/pgr-a/ota", "sup", {"target": target})


def test_a_build_without_mv_is_offered_to_a_capable_pager_that_has_not_moved(ota):
    assert _device(ota).move.state == "waiting"
    assert _push(ota, ID_NEW[:16]).status_code == 200


def test_a_build_without_mv_is_not_offered_to_a_pager_that_has_moved(ota):
    _arrive(ota, mv_st="ok")
    r = _push(ota, ID_NEW[:16])
    assert r.status_code == 422 and ID_NEW[:16] in r.json()["detail"]


def test_a_build_with_mv_is_offered(ota):
    ota.index["index"]["builds"][0]["caps"] = ["mv"]
    firmware.reset_cache()
    assert _push(ota, ID_NEW[:16]).status_code == 200


def test_a_moved_pager_without_mvc_is_covered_too(ota):
    _arrive(ota, mv_st="ok", mvc=False)
    assert _device(ota).move.state == "moved"
    r = _push(ota, ID_NEW[:16])
    assert r.status_code == 422


def test_a_pager_that_never_took_part_in_a_move_gets_any_build(net, make_relay, index_server):  # noqa: F811
    world = make_world(net, make_relay, fw_index_url=INDEX_URL, fw_bucket_base=BASE_URL)
    world.dst.family("f", "F", federated=False)
    world.dst.user("boss", "boss", "super", "f")
    world.dst.user("o", "o", "member", "f")
    with world.dst.active():
        devices_store.create_device(
            device_id="pgr-plain", owner_uid="o", label="l", mqtt_username="pgr-plain",
            mqtt_password_hash="h", family_id="f",
        )
        devices_store.update_status("pgr-plain", state="online", otaCap=1)
    r = world.dst.post("/api/admin/devices/pgr-plain/ota", "boss", {"target": ID_NEW[:16]})
    assert r.status_code == 200, r.text


def test_a_job_with_a_foreign_url_is_cancelled_and_issued_again_at_the_commit(ota):
    ota.index["index"]["builds"][0]["caps"] = ["mv"]
    firmware.reset_cache()
    job = {
        "target16": ID_NEW[:16], "kind": "full", "osz": 338784, "estBytes": 1, "by_uid": "sup",
        "onDemand": False,
    }
    with ota.dst.active():
        devcfg.push_ota(
            "pgr-a",
            {"img": ID_NEW, "url": "https://old-bucket.example/fw/x/full.z", "osz": 1, "osha": "d4" * 32},
            ota.dst.broker,
            job=job,
        )
        ota.dst.broker.clear()
    pager, _n = _arrive(ota, mv_st="ok")
    sent = [e for e in published(ota.dst, "pgr-a", pager.k_prime) if e.get("kind") == "cfg" and "ota" in e["cfg"]]
    # the arriving pager is not re-sent the unacked foreign job: it is cancelled and issued again at
    # the commit
    assert [("cancel" in e["cfg"]["ota"]) for e in sent] == [True, False]
    assert sent[-1]["cfg"]["ota"]["url"].startswith(BASE_URL)
    assert _device(ota).otaJob.target16 == ID_NEW[:16]


def test_a_job_with_this_servers_url_is_left_alone(ota):
    ota.index["index"]["builds"][0]["caps"] = ["mv"]
    firmware.reset_cache()
    job = {
        "target16": ID_NEW[:16], "kind": "full", "osz": 1, "estBytes": 1, "by_uid": "sup",
        "onDemand": False,
    }
    with ota.dst.active():
        devcfg.push_ota(
            "pgr-a", {"img": ID_NEW, "url": BASE_URL + "fw/x/full.z", "osz": 1, "osha": "d4" * 32},
            ota.dst.broker, job=job,
        )
        ota.dst.broker.clear()
    pager, _n = _arrive(ota, mv_st="ok")
    sent = [e for e in published(ota.dst, "pgr-a", pager.k_prime) if "ota" in e.get("cfg", {})]
    assert not any("cancel" in e["cfg"]["ota"] for e in sent)
    assert _device(ota).otaJob is not None


def test_a_foreign_job_for_a_build_without_mv_is_cancelled_and_not_reissued(ota):
    job = {
        "target16": ID_NEW[:16], "kind": "full", "osz": 1, "estBytes": 1, "by_uid": "sup",
        "onDemand": False,
    }
    with ota.dst.active():
        devcfg.push_ota(
            "pgr-a", {"img": ID_NEW, "url": "https://old.example/fw/x/full.z", "osz": 1, "osha": "d4" * 32},
            ota.dst.broker, job=job,
        )
        ota.dst.broker.clear()
    pager, _n = _arrive(ota, mv_st="ok")
    sent = [e for e in published(ota.dst, "pgr-a", pager.k_prime) if "ota" in e.get("cfg", {})]
    assert [("cancel" in e["cfg"]["ota"]) for e in sent] == [True]
    assert _device(ota).otaJob is None


# ---- guards ------------------------------------------------------------------------------------


def test_delivery_is_held_only_for_a_waiting_imported_pager(live):
    with live.dst.active():
        assert move_module.delivery_held("pgr-a") is True
        assert move_module.delivery_held("nope") is False
        assert move_module.delivery_held(None) is False
    _arrive(live, mv_st="pend")
    with live.dst.active():
        assert move_module.delivery_held("pgr-a") is False


def test_an_empty_firebase_target_has_no_open_import(world, monkeypatch):
    """Only home sign-in has an open first-run page; elsewhere an import without a super is a 401."""
    from app.routers import move as move_router

    world.freeze()
    data = world.bundle()
    monkeypatch.setattr(move_router, "auth_backend", lambda: "firebase")
    assert world.import_bundle(data).status_code == 401
    with world.dst.active():
        assert get_db().collection("settings").document("move").get().exists is False


# ---- commit is one check-and-set ---------------------------------------------------------------


def test_the_proofs_ack_and_an_ok_report_commit_once(live, monkeypatch):
    from app import move_target

    pager, _n = _arrive(live)
    proof_env = next(
        e for e in published(live.dst, "pgr-a", pager.k_prime) if e.get("kind") == "cfg" and "mv" in e["cfg"]
    )
    reissued: list[str] = []
    monkeypatch.setattr(move_target, "reissue_ota", lambda device_id, *_a: reissued.append(device_id))
    pager.ack(live.dst, pager.k_prime, proof_env["id"])
    pager.status(live.dst, pager.k_prime, mv={"id": live.move_id, "st": "ok", "at": 1})
    dev = _device(live)
    assert (dev.provisionState, dev.move.state) == ("provisioned", "moved")
    assert reissued == ["pgr-a"]
    moved_at = dev.move.movedAt

    with live.dst.active():
        assert move_target.commit("pgr-a", live.dst.broker) is False
    assert reissued == ["pgr-a"] and _device(live).move.movedAt == moved_at


def test_a_commit_for_a_pager_whose_move_is_not_open_does_nothing(live):
    from app import move_target

    with live.dst.active():
        assert move_target.commit("pgr-a", live.dst.broker) is True
        get_db().collection("devices").document("pgr-a").update(
            {"move.state": "reprovisioned", "provisionState": "issued"}
        )
        assert move_target.commit("pgr-a", live.dst.broker) is False
        assert move_target.commit("pgr-nope", live.dst.broker) is False
    dev = _device(live)
    assert (dev.provisionState, dev.move.state) == ("issued", "reprovisioned")


# ---- the people and Beam rows ------------------------------------------------------------------


def test_the_people_rows_say_who_has_no_sign_in_and_when_people_signed_in(live):
    with live.dst.active():
        move_module.patch_record(noSignIn=["kid"])
    rows = {p["alias"]: p for p in live.dst.get("/api/admin/move", "sup").json()["people"]}
    assert rows["kid"]["noSignIn"] is True and rows["mom"]["noSignIn"] is False
    assert rows["mom"]["signedIn"] is True and rows["mom"]["lastSignInAt"] is not None
    with live.dst.active():
        from app.store import users as users_store

        users_store.create_user(uid="zed", alias="zed", display_name="Zed", role="member", family_id="fam")
    rows = {p["alias"]: p for p in live.dst.get("/api/admin/move", "sup").json()["people"]}
    assert rows["zed"]["signedIn"] is False and rows["zed"]["lastSignInAt"] is None


def test_an_import_records_who_got_no_way_to_sign_in(world):
    world.freeze(accounts=[{"uid": "mom", "noSignIn": True}])
    assert world.import_bundle(world.bundle()).status_code == 201
    with world.dst.active():
        rec = move_module.get_record()
    # a home target: no claim code and no password is no way in
    assert rec.noSignIn == ["mom"]


def test_a_beam_pager_is_remembered_after_it_reports_another_bearer(beam):
    beam.dst.post("/api/admin/move/beam/switch", "sup", {})
    pager, _n = _arrive(beam, tls="proxy")
    assert _device(beam).move.onBeam is True
    pager.status(beam.dst, pager.k_prime, mv={"id": beam.move_id, "st": "ok", "at": 1}, tls="unpinned",
                 session="s_00000002")
    assert _device(beam).move.state == "moved" and _device(beam).status.tls == "unpinned"
    block = beam.dst.get("/api/admin/move", "sup").json()["beam"]
    assert block["count"] == 1 and block["movedCount"] == 1
    assert beam.dst.post("/api/admin/move/beam/switch-back", "sup", {}).status_code == 409


def test_a_direct_pager_is_not_marked_as_on_beam(live):
    _arrive(live)
    assert _device(live).move.onBeam is False


def test_the_firebase_sign_in_lookup_is_batched(live, monkeypatch):
    from types import SimpleNamespace

    from firebase_admin import auth as fb_auth

    from app import move_target

    monkeypatch.setattr(move_target, "auth_backend", lambda: "firebase")
    calls: list[list[str]] = []

    def get_users(identifiers):
        calls.append([i.uid for i in identifiers])
        stamp = int(datetime(2030, 1, 1, tzinfo=UTC).timestamp() * 1000)
        return SimpleNamespace(
            users=[SimpleNamespace(uid="mom", user_metadata=SimpleNamespace(last_sign_in_timestamp=stamp))]
        )

    monkeypatch.setattr(fb_auth, "get_users", get_users, raising=False)
    monkeypatch.setattr(fb_auth, "get_user", lambda *_a: pytest.fail("one lookup per person"), raising=False)
    with live.dst.active():
        rows = {p["alias"]: p for p in move_target._people_rows(move_module.get_record())}
    assert len(calls) == 1 and sorted(calls[0]) == ["kid", "mom", "sup"]
    assert rows["mom"]["signedIn"] is True and rows["mom"]["lastSignInAt"].startswith("2030-01-01")
    assert rows["kid"]["signedIn"] is False and rows["kid"]["lastSignInAt"] is None


# ---- an import that stopped half way -----------------------------------------------------------


def test_move_info_does_not_report_an_import_that_has_not_finished(world, monkeypatch):
    from app import deployexport

    world.move_id = world.freeze()
    data = world.bundle()

    def boom(*_a, **_k):
        raise RuntimeError("stopped")

    with monkeypatch.context() as m:
        m.setattr(deployexport, "_clear_placeholder", boom)
        with pytest.raises(RuntimeError):
            world.import_bundle(data)
    info = world.dst.raw("GET", "/api/public/move-info").json()
    assert info["move"] is None and info["empty"] is False
    with world.dst.active():
        assert move_module.get_record().state == "importing"

    # the same bundle finishes it, with nobody signed in: the bundle and its passphrase are the credential
    again = world.import_bundle(data)
    assert again.status_code == 201, again.text
    info = world.dst.raw("GET", "/api/public/move-info").json()
    assert info["move"] == {"id": world.move_id, "imported": True}
    with world.dst.active():
        assert move_module.get_record().state == "imported"


def test_an_unfinished_import_takes_no_other_bundle_and_no_move_actions(world, monkeypatch):
    from app import deployexport

    world.move_id = world.freeze()
    data = world.bundle()

    def boom(*_a, **_k):
        raise RuntimeError("stopped")

    with monkeypatch.context() as m:
        m.setattr(deployexport, "_clear_placeholder", boom)
        with pytest.raises(RuntimeError):
            world.import_bundle(data)
    # a bundle made for another move is refused with the report
    with world.dst.active():
        get_db().collection("settings").document("move").update({"id": "mv_aaaaaaaaaaaa"})
    r = world.import_bundle(data)
    assert r.status_code == 409 and any("unfinished import" in e for e in r.json()["errors"])
    # nothing of the move can be run until the import has finished
    with world.dst.active():
        get_db().collection("settings").document("move").update({"id": world.move_id})
    world.dst.sign_in("sup")
    assert world.dst.post("/api/admin/move/end", "sup", {}).status_code == 409


def test_foreign_downloads_are_told_apart_from_this_servers_own():
    from app import move_target
    from app.config import Settings

    settings = dataclasses.replace(Settings.from_env(), fw_bucket_base="https://bucket.example/fw/")
    own, foreign = "https://bucket.example/fw/x.z", "https://elsewhere.example/fw/x.z"
    for key in ("ota", "ast"):
        assert move_target.foreign_download(settings, {"cfg": {key: {"url": foreign}}}) is True
        assert move_target.foreign_download(settings, {"cfg": {key: {"url": own}}}) is False
        assert move_target.foreign_download(settings, {"cfg": {key: {"cancel": True}}}) is False
    assert move_target.foreign_download(settings, {"cfg": {"mv": {"id": "x"}}}) is False
    assert move_target.foreign_download(settings, {"kind": "book"}) is False


def test_a_build_without_mv_is_refused_only_to_a_pager_that_has_moved():
    build = firmware.FwBuild.model_construct(id="ab" * 32, version="1", caps=[])
    assert firmware.move_refusal(build, move_state="moved") is not None
    for state in (None, "waiting", "trying", "reprovisioned"):
        assert firmware.move_refusal(build, move_state=state) is None
    build.caps = ["mv"]
    assert firmware.move_refusal(build, move_state="moved") is None
