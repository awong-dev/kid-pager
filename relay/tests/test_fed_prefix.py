"""Federation when a relay is mounted under a URL path prefix (`PAGER_PATH=/pager`): the peer's
coordinates are host + port + path, the URL carries the prefix, the signature covers the route the
relay sees after its proxy strips it. The test transport plays nginx (404 outside the prefix,
strip inside), so a client that forgets the prefix fails."""

from __future__ import annotations

import base64
import io
import json
import time

import httpx
import pytest

from app import familyexport
from app.config import valid_base_path
from app.familyexport import ExportOptions, ImportOptions, KpfamFile
from app.fed import keys, linking, sign
from app.fed import sender as fed_sender
from app.store import alerts as alerts_store
from app.store import fed_links, fed_outbox
from app.store import messages as messages_store
from tests.fed_world import (
    identity_of,
    link_families,
    needs_two_relays,
    signed_call,
)

pytestmark = needs_two_relays

FED = "/api/family/fed"
PASS = "correct horse"


@pytest.fixture
def world(make_relay):
    """x at relay-x.example.org/pager, y at relay-y.example.org (root), z at
    relay-z.example.org/kids/pager (the move target)."""
    x = make_relay("relay-x", base_path="/pager")
    y = make_relay("relay-y")
    z = make_relay("relay-z", base_path="/kids/pager")
    for r in (x, y, z):
        r.enable_server()
    x.family("famA", "The Alphas")
    y.family("famB", "The Betas")
    z.family("famRoot", "Root", federated=False)
    x.user("adm_a", "adma", "admin", "famA", "Ada Admin")
    x.user("ann", "ann", "member", "famA", "Ann A")
    y.user("adm_b", "admb", "admin", "famB", "Bea Admin")
    y.user("zed", "zed", "member", "famB", "Zed B")
    z.user("root", "root", "super", "famRoot")
    return x, y, z


def linked(x, y):
    lx, ly = link_families(x, "adm_a", y, "adm_b")
    x.put(f"{FED}/links/{lx['id']}/shared", "adm_a", {"shared": ["ann"]})
    y.put(f"{FED}/links/{ly['id']}/shared", "adm_b", {"shared": ["zed"]})
    return lx, ly


def befriend(x, y, lx):
    r = x.post(f"{FED}/links/{lx['id']}/contacts", "adm_a", {"rid": "zed", "members": ["ann"]})
    assert r.status_code == 201, r.text
    with y.active():
        alert = next(a for a in alerts_store.list_alerts("famB") if a.kind == "fed_contact")
    assert y.post(f"/api/family/alerts/{alert.id}/approve", "adm_b", {}).status_code == 200


def test_the_path_validator_is_the_pager_path_rule():
    for ok in ("", "/pager", "/a/b", "/a_b-c/9"):
        assert valid_base_path(ok)
    for bad in (
        "/", "pager", "/pager/", "/..", "/a/../b", "/a.b", "/a?x=1", "/a#f", "/a%2f", "/a b",
        "/a\n", "/a\t", "//evil.example", "/a//b", "https://evil.example", "@evil", "/é",
        "/" + "a" * 250,
    ):
        assert not valid_base_path(bad), bad


def test_invite_link_directory_and_messages_across_a_prefix(world, net):
    x, y, _z = world
    lx, ly = linked(x, y)
    ident_a, ident_b = identity_of(x, "famA"), identity_of(y, "famB")

    # the coordinates are stored and shown with their path
    assert lx["remote"]["path"] == "" and lx["remote"]["host"] == "relay-y.example.org"
    assert ly["remote"]["path"] == "/pager" and ly["remote"]["host"] == "relay-x.example.org"
    with y.active():
        link_on_y = fed_links.get_for("famB", ident_a.fedId)
        assert link_on_y.remote.path == "/pager"
    # the link request went to /pager/fed/v1/link and the issuer saw the bare route
    (hit,) = [h for h in net.hits if h.path.endswith("/fed/v1/link")]
    assert hit.path == "/pager/fed/v1/link" and hit.status == 200
    assert hit.host_header == "relay-x.example.org" and hit.sni == "relay-x.example.org"
    # B's side is at the root: A's calls to B carry no prefix
    net.clear()
    befriend(x, y, lx)
    assert {h.path for h in net.to("relay-x.example.org")} <= {
        "/pager/fed/v1/inbox",
        "/pager/fed/v1/directory",
    }
    assert net.to("relay-x.example.org") and all(h.status == 200 for h in net.hits)
    assert all(h.path.startswith("/fed/v1/") for h in net.to("relay-y.example.org"))

    # a message each way
    net.clear()
    sent = y.post("/api/conversations/ann/messages", "zed", {"body": "hi under a prefix"})
    assert sent.status_code == 201
    with y.active():
        fed_sender.tick_pass()
        assert fed_outbox.list_pending(f"famB_{ident_a.fedId}") == []
    (inbox,) = [h for h in net.hits if h.path.endswith("/inbox")]
    assert inbox.path == "/pager/fed/v1/inbox" and inbox.status == 200
    back = x.post("/api/conversations/zed/messages", "ann", {"body": "and back"})
    assert back.status_code == 201
    with x.active():
        fed_sender.tick_pass()
    assert net.to("relay-y.example.org")[-1].path == "/fed/v1/inbox"
    with x.active():
        from app.store import externals as externals_store

        remote = next(iter(externals_store.list_fed_externals("famA")))
        bodies = [m.body for m in messages_store.list_thread(messages_store.conv_key(remote.uid, "ann"))]
    assert "hi under a prefix" in bodies
    with y.active():
        from app.store import externals as externals_store

        remote = next(iter(externals_store.list_fed_externals("famB")))
        bodies = [m.body for m in messages_store.list_thread(messages_store.conv_key(remote.uid, "zed"))]
    assert "and back" in bodies
    assert ident_b


def test_the_directory_is_reachable_only_with_the_prefix(world, net):
    x, y, _z = world
    linked(x, y)
    a_id = identity_of(y, "famB").fedId
    # the client signs the bare route (what x sees after the proxy) and sends it to /pager/...
    ok = signed_call(y, "famB", x, identity_of(x, "famA").fedId, "GET", "/fed/v1/directory")
    assert ok.status_code == 200
    assert a_id
    # a request without the prefix never reaches x's app
    net.clear()
    client = net.client()
    reply = client.call("GET", "/fed/v1/directory", host="relay-x.example.org", base_path="")
    assert reply.status == 404 and net.hits[-1].path == "/fed/v1/directory"


def test_the_signature_covers_the_route_the_relay_sees_not_the_prefixed_path(world, net):
    x, y, _z = world
    linked(x, y)
    ident_a, ident_b = identity_of(x, "famA"), identity_of(y, "famB")
    net.clear()
    with y.active():
        link = fed_links.get_for("famB", ident_a.fedId)
        reply = net.client().directory(ident_b, link)
    assert [e.alias for e in reply] == ["ann"]
    (hit,) = net.hits
    assert hit.path == "/pager/fed/v1/directory" and hit.status == 200
    # recompute what was signed: the bare route verifies, the prefixed path does not
    parsed = sign.parse_headers(httpx.Headers(hit.headers))
    pub = keys.public_from_b64(ident_b.publicKey)
    sign.verify(pub, parsed, "GET", "/fed/v1/directory", b"")
    with pytest.raises(sign.SignatureError):
        sign.verify(pub, parsed, "GET", "/pager/fed/v1/directory", b"")
    # and a request signed over the prefixed path is refused by the relay behind the proxy
    headers = sign.sign(
        keys.private_from_b64(ident_b.privateKey),
        ident_b.fedId,
        ident_a.fedId,
        "GET",
        "/pager/fed/v1/directory",
    )
    assert x.raw("GET", "/fed/v1/directory", headers=headers).status_code == 403


def test_the_invite_blob_and_party_carry_the_path(world):
    x, y, _z = world
    blob = linking.decode_invite(x.post(f"{FED}/invites", "adm_a").json()["invite"])
    assert (blob.host, blob.port, blob.path) == ("relay-x.example.org", 443, "/pager")
    assert linking.PREFIX == "kpfed3."
    blob_y = linking.decode_invite(y.post(f"{FED}/invites", "adm_b").json()["invite"])
    assert blob_y.path == ""


def _invite_with(blob_path, x):
    invite = x.post(f"{FED}/invites", "adm_a").json()["invite"]
    raw = json.loads(base64.urlsafe_b64decode(invite[len("kpfed3.") :] + "=="))
    if blob_path is None:
        raw.pop("path")
    else:
        raw["path"] = blob_path
    enc = base64.urlsafe_b64encode(json.dumps(raw).encode()).decode().rstrip("=")
    return "kpfed3." + enc


@pytest.mark.parametrize(
    "bad", [None, "/..", "/a/../b", "/a?x=1", "/a#f", "/a%2e", "/a b", "//evil.example", "/pager/", "x"]
)
def test_a_malformed_peer_path_in_an_invite_is_refused_before_any_request(world, net, bad):
    x, y, _z = world
    net.clear()
    out = y.post(f"{FED}/links", "adm_b", {"invite": _invite_with(bad, x)})
    assert out.status_code == 400
    assert net.hits == []


def test_an_old_format_invite_is_not_an_invite(world):
    x, y, _z = world
    invite = x.post(f"{FED}/invites", "adm_a").json()["invite"]
    out = y.post(f"{FED}/links", "adm_b", {"invite": "kpfed2." + invite[len("kpfed3.") :]})
    assert out.status_code == 400


def test_a_malformed_peer_path_in_a_link_request_or_moved_is_400(world):
    x, y, _z = world
    lx, _ly = linked(x, y)
    ident_a, ident_b = identity_of(x, "famA"), identity_of(y, "famB")
    for bad in ("/..", "/a?x", "//h", "/p/", "pager"):
        moved = {
            "fedId": ident_b.fedId, "epoch": ident_b.epoch + 1, "pk": ident_b.publicKey,
            "host": "relay-y.example.org", "port": 443, "path": bad,
        }
        reply = signed_call(y, "famB", x, ident_a.fedId, "POST", "/fed/v1/moved", moved)
        assert reply.status_code == 400, bad
    # a stored link row with a bad path does not load
    with pytest.raises(ValueError):
        fed_links.Remote(fedId="f", name="n", publicKey="k", host="h", path="/../x")
    assert lx


def test_the_client_refuses_a_bad_path_and_keeps_the_address_rules(world, net):
    from app.fed.client import FedTransportError

    c = net.client()
    for bad in ("/..", "/a?x", "//evil.example", "@evil.example", "/a b", "/a/"):
        with pytest.raises(FedTransportError):
            c.call("GET", "/fed/v1/info", host="relay-x.example.org", base_path=bad)
    for route in ("//evil.example/fed/v1/x", "@evil/fed/v1", "/api/admin"):
        with pytest.raises(FedTransportError):
            c.call("GET", route, host="relay-x.example.org", base_path="/pager")
    # the checked address still decides where the request goes
    net.dns["relay-x.example.org"] = ["127.0.0.1"]
    net.clear()
    reply = c.call("GET", "/fed/v1/info", host="relay-x.example.org", base_path="/pager")
    assert reply.status == 200
    (hit,) = net.hits
    assert hit.ip == "127.0.0.1" and hit.host_header == "relay-x.example.org"
    assert hit.path == "/pager/fed/v1/info"


def export_bytes(relay, fid="famA", **opts) -> bytes:
    buf = io.BytesIO()
    with relay.active():
        familyexport.write_kpfam(
            relay.db, fid, buf, passphrase=PASS, opts=ExportOptions(**opts)
        )
    return buf.getvalue()


def test_a_link_path_survives_backup_and_restore(world):
    x, y, z = world
    linked(x, y)
    fed_a = identity_of(x, "famA").fedId
    # y -> x link stores /pager; back up famB and restore it on z
    data = export_bytes(y, "famB", backup=True)
    with z.active():
        familyexport.apply_import(
            z.db, KpfamFile(io.BytesIO(data), PASS), ImportOptions(target_kind="home")
        )
        link = fed_links.get_for("famB", fed_a)
    assert link.remote.path == "/pager" and link.remote.host == "relay-x.example.org"
    assert link.state == "reauth"


def test_a_tampered_path_in_an_import_file_is_refused(world):
    x, y, _z = world
    linked(x, y)
    with y.active():
        (link,) = fed_links.list_for_family("famB")
        row = y.db.collection("fedLinks").document(link.id).get().to_dict()
    good = {"path": f"fedLinks/{link.id}", "data": row}
    assert familyexport._scan([good]).fed_links == 1
    for bad in ("/..", "/a?x", "//evil.example", "/a%2e", "/a b"):
        tampered = {**row, "remote": {**row["remote"], "path": bad}}
        with pytest.raises(familyexport.CorruptFile):
            familyexport._scan([{"path": good["path"], "data": tampered}])


def test_a_move_to_a_new_host_and_path_updates_the_peer_and_messages_follow(world, net):
    x, y, z = world
    lx, _ly = linked(x, y)
    befriend(x, y, lx)
    ident_old = identity_of(x, "famA")
    fed_b = identity_of(y, "famB").fedId
    y_link = f"famB_{ident_old.fedId}"
    with y.active():
        assert fed_links.get(y_link).remote.path == "/pager"

    with x.active():
        from app.store import families as families_store

        families_store.set_frozen("famA", True)
    data = export_bytes(x, "famA")
    with z.active():
        familyexport.apply_import(
            z.db, KpfamFile(io.BytesIO(data), PASS), ImportOptions(target_kind="home")
        )
        # the link to y is stored with y's (root) path, unchanged by the move
        assert fed_links.get_for("famA", fed_b).remote.path == ""

    net.clear()
    with z.active():
        fed_sender.tick_pass()
    (moved_hit,) = [h for h in net.hits if h.path.endswith("/moved")]
    assert moved_hit.path == "/fed/v1/moved" and moved_hit.status == 200  # z -> y (root)
    with y.active():
        got = fed_links.get(y_link)
        assert (got.remote.host, got.remote.port, got.remote.path) == (
            "relay-z.example.org",
            443,
            "/kids/pager",
        )
        (alert,) = [a for a in alerts_store.list_alerts("famB") if a.kind == "fed_moved"]
        assert alert.fedOldHost == "relay-x.example.org/pager"
        assert alert.fedHost == "relay-z.example.org/kids/pager"
        assert "relay-x.example.org/pager" in alert.preview
        assert "relay-z.example.org/kids/pager" in alert.preview

    # y now dials z under its prefix; the old home is no longer used
    net.clear()
    sent = y.post("/api/conversations/ann/messages", "zed", {"body": "welcome to /kids/pager"})
    assert sent.status_code == 201
    with y.active():
        fed_sender.tick_pass(now=time.time() + 3600)
        assert fed_outbox.list_pending(y_link) == []
    to_z = [h.path for h in net.hits if h.host_header.startswith("relay-z")]
    assert to_z and set(to_z) == {"/kids/pager/fed/v1/inbox"}
    assert not net.to("relay-x.example.org")
    with z.active():
        from app.store import users as users_store

        ext = next(u for u in users_store.list_users() if u.ownerFamilyId == "famA" and u.fed)
        (m,) = messages_store.list_thread(messages_store.conv_key(ext.uid, "ann"))
        assert m.body == "welcome to /kids/pager"


def test_a_moved_that_changes_only_the_path_is_a_change_not_a_repeat(world, net):
    x, y, _z = world
    linked(x, y)
    ident_a = identity_of(x, "famA")
    fed_b = identity_of(y, "famB").fedId
    body = {
        "fedId": ident_a.fedId, "epoch": ident_a.epoch + 1,
        "pk": keys.generate()[1], "host": "relay-x.example.org", "port": 443, "path": "/pager",
    }
    with y.active():
        before = fed_links.get(f"famB_{ident_a.fedId}")
    assert before.remote.path == "/pager"
    # same epoch, same everything but the path: not a retry of the applied announcement
    from app.fed import moved

    same_epoch = {**body, "epoch": before.remote.epoch, "pk": before.remote.publicKey}
    with y.active():
        assert moved.handle_moved("famB", before, json.dumps(same_epoch).encode(), used_prev_key=False) == before
        with pytest.raises(moved.MovedError) as exc:
            moved.handle_moved(
                "famB", before, json.dumps({**same_epoch, "path": "/other"}).encode(),
                used_prev_key=False,
            )
        assert exc.value.status == 409
    assert fed_b
