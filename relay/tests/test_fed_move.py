"""A family move carries the federation identity and links.

Three real relays: X (the family's old home), Z (its new one), Y (a family it is linked to)."""

from __future__ import annotations

import io

import pytest

from app import familyexport
from app.familyexport import ExportOptions, ImportOptions, KpfamFile
from app.fed import keys, sign
from app.fed import sender as fed_sender
from app.store import alerts as alerts_store
from app.store import fed_families, fed_links, fed_outbox
from app.store import messages as messages_store
from app.store import users as users_store
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
    x, y, z = make_relay("relay-x"), make_relay("relay-y"), make_relay("relay-z")
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
    lx, ly = link_families(x, "adm_a", y, "adm_b")
    x.put(f"{FED}/links/{lx['id']}/shared", "adm_a", {"shared": ["ann"]})
    y.put(f"{FED}/links/{ly['id']}/shared", "adm_b", {"shared": ["zed"]})
    out = x.post(f"{FED}/links/{lx['id']}/contacts", "adm_a", {"rid": "zed", "members": ["ann"]}).json()
    with y.active():
        alert = next(a for a in alerts_store.list_alerts("famB") if a.kind == "fed_contact")
    y.post(f"/api/family/alerts/{alert.id}/approve", "adm_b", {})
    return x, y, z, out["alias"]


def export_bytes(relay, fid="famA", *, passphrase=PASS, **opts) -> bytes:
    buf = io.BytesIO()
    with relay.active():
        familyexport.write_kpfam(
            relay.db, fid, buf, passphrase=passphrase, opts=ExportOptions(**opts)
        )
    return buf.getvalue()


def move(x, z, fid="famA", *, as_id=None, enable_server=True):
    """Freeze on X, export, import on Z. Returns the import result."""
    with x.active():
        from app.store import families as families_store

        families_store.set_frozen(fid, True)
    data = export_bytes(x, fid)
    with z.active():
        return familyexport.apply_import(
            z.db,
            KpfamFile(io.BytesIO(data), PASS),
            ImportOptions(as_id=as_id, target_kind="home"),
        )


def test_a_move_export_refuses_a_key_without_a_passphrase(world):
    x, _y, _z, _alias = world
    with x.active():
        from app.store import families as families_store

        families_store.set_frozen("famA", True)
        with pytest.raises(familyexport.PassphraseRequired) as exc:
            familyexport.export_family(x.db, "famA", ExportOptions(), encrypted=False)
        assert "private key" in str(exc.value)
        buf = io.BytesIO()
        with pytest.raises(familyexport.PassphraseRequired):
            familyexport.write_kpfam(x.db, "famA", buf, passphrase=None, opts=ExportOptions())


def test_the_http_move_export_needs_the_passphrase_header(world):
    x, _y, _z, _alias = world
    x.family("famS", "S")
    x.user("root_x", "rootx", "super", "famS")
    with x.active():
        from app.store import families as families_store

        families_store.set_frozen("famA", True)
    assert x.get("/api/admin/families/famA/export", "root_x").status_code == 400
    with x.active():
        resp = x.http.get(
            "/api/admin/families/famA/export",
            headers={**x.auth("root_x"), "X-Export-Passphrase": PASS},
        )
    assert resp.status_code == 200
    f = KpfamFile(io.BytesIO(resp.content), PASS)
    assert f.manifest().kind == "move"


def test_a_move_file_holds_the_private_key_and_the_links(world):
    x, _y, _z, _alias = world
    with x.active():
        from app.store import families as families_store

        families_store.set_frozen("famA", True)
    data = export_bytes(x)
    f = KpfamFile(io.BytesIO(data), PASS)
    rows = {r["path"]: r["data"] for r in f.rows()}
    ident = identity_of(x, "famA")
    assert rows["fedFamilies/famA"]["privateKey"] == ident.privateKey
    assert any(p.startswith("fedLinks/famA_") for p in rows)
    assert rows["families/famA"]["federation"] is True  # the flag travels
    assert f.manifest().counts["fedLinks"] == 1


def test_person_uids_and_federated_contact_uids_survive_an_import_under_another_id(world):
    x, _y, z, _alias = world
    with x.active():
        before = sorted(u.uid for u in users_store.list_users() if u.familyId == "famA" or u.ownerFamilyId == "famA")
    move(x, z, as_id="famA2")
    with z.active():
        after = sorted(
            u.uid for u in users_store.list_users() if u.familyId == "famA2" or u.ownerFamilyId == "famA2"
        )
        assert after == before
        ident = fed_families.get("famA2")
        assert ident.fedId == identity_of(x, "famA").fedId and ident.familyId == "famA2"
        (link,) = fed_links.list_for_family("famA2")
        assert link.id.startswith("famA2_") and link.familyId == "famA2" and link.shared == ["ann"]


def test_move_end_to_end(world, net):
    x, y, z, _alias = world
    ident_old = identity_of(x, "famA")
    fed_b = identity_of(y, "famB").fedId
    y_link = f"famB_{ident_old.fedId}"

    # a message queued on Y while X is unreachable: it must follow the move
    net.down.add("relay-x.example.org")
    sent = y.post("/api/conversations/ann/messages", "zed", {"body": "while you were moving"})
    assert sent.status_code == 201
    mid = sent.json()["id"]
    with y.active():
        assert [r.event["to"] for r in fed_outbox.list_pending(y_link)] == ["ann"]

    result = move(x, z)
    assert result.report.kind == "move"
    assert result.report.federation.model_dump() == {"links": 1, "reauth": 0, "announcing": 1}
    assert result.report.ok

    with z.active():
        ident_new = fed_families.get("famA")
        link = fed_links.get_for("famA", fed_b)
        assert ident_new.fedId == ident_old.fedId  # stable across the move
        assert ident_new.epoch == ident_old.epoch + 1
        assert ident_new.publicKey != ident_old.publicKey
        assert ident_new.prevPrivateKey == ident_old.privateKey  # kept to sign the announcement
        assert link.announce_pending and link.state == "active" and link.shared == ["ann"]

    # Y does not know yet: it still has the old key and address
    with y.active():
        assert fed_links.get(y_link).remote.host == "relay-x.example.org"

    # Z announces (the tick), signed with the OLD key; Y accepts and raises the alert
    with z.active():
        fed_sender.tick_pass()
    with y.active():
        got = fed_links.get(y_link)
        assert (got.remote.host, got.remote.epoch, got.remote.publicKey) == (
            "relay-z.example.org",
            ident_new.epoch,
            ident_new.publicKey,
        )
        assert got.remote.prevPublicKey == ident_old.publicKey
        moved_alerts = [a for a in alerts_store.list_alerts("famB") if a.kind == "fed_moved"]
        (alert,) = moved_alerts
        assert alert.fedName == "The Alphas" and alert.fedHost == "relay-z.example.org"
        assert alert.fedOldHost == "relay-x.example.org"
        assert "Suspend the link if you did not expect this" in alert.preview
    with z.active():
        assert fed_links.get_for("famA", fed_b).announce is None
        assert fed_families.get("famA").prevPrivateKey is None  # all announced: the old key goes

    # the queued row on Y went to the new address, and Z stored it for ann
    with y.active():
        fed_sender.tick_pass(now=__import__("time").time() + 3600)
        assert fed_outbox.list_pending(y_link) == []
    assert [h for h in net.to("relay-z.example.org") if h.path == "/fed/v1/inbox"]
    with y.active():
        d = messages_store.get_message(mid).deliveries["fed"]
        assert d.state == "sent" and d.serverHost == "relay-x.example.org"
    with z.active():
        ext = next(u for u in users_store.list_users() if u.ownerFamilyId == "famA" and u.fed)
        (m,) = messages_store.list_thread(messages_store.conv_key(ext.uid, "ann"))
        assert m.body == "while you were moving"

    # the old copy is dead: its signatures no longer verify, even unfrozen again
    dead = signed_call(x, "famA", y, fed_b, "GET", "/fed/v1/directory")
    assert dead.status_code == 403
    with x.active():
        from app.store import families as families_store

        families_store.set_frozen("famA", False)
    assert signed_call(x, "famA", y, fed_b, "GET", "/fed/v1/directory").status_code == 403
    # while the new copy works
    live = signed_call(z, "famA", y, fed_b, "GET", "/fed/v1/directory")
    assert live.status_code == 200 and [e["alias"] for e in live.json()] == ["zed"]
    # and Y -> Z messages flow both ways from now on
    assert y.post("/api/conversations/ann/messages", "zed", {"body": "welcome"}).status_code == 201


def moved_body(ident, host="relay-z.example.org", epoch=None, pk=None):
    return {
        "fedId": ident.fedId,
        "epoch": ident.epoch if epoch is None else epoch,
        "pk": pk or ident.publicKey,
        "host": host,
        "port": 443,
        "path": "",
    }


def test_moved_endpoint_rules(world, net):
    x, y, z, _alias = world
    old = identity_of(x, "famA")
    fed_b = identity_of(y, "famB").fedId
    move(x, z)
    with z.active():
        new = fed_families.get("famA")
    body = moved_body(new)
    path = "/fed/v1/moved"

    # a suspended link refuses even the genuine announcement
    with y.active():
        fed_links.set_state(f"famB_{old.fedId}", "suspended")
    r = signed_call(z, "famA", y, fed_b, "POST", path, body, private_key_b64=old.privateKey)
    assert r.status_code == 403
    with y.active():
        fed_links.set_state(f"famB_{old.fedId}", "active")

    # the genuine announcement, by hand, signed with the old key
    first = signed_call(z, "famA", y, fed_b, "POST", path, body, private_key_b64=old.privateKey)
    assert first.status_code == 200
    with y.active():
        assert len([a for a in alerts_store.list_alerts("famB") if a.kind == "fed_moved"]) == 1

    # a retry after a lost ack: same epoch, identical content, still the old key: idempotent ok,
    # and no second alert
    again = signed_call(z, "famA", y, fed_b, "POST", path, body, private_key_b64=old.privateKey)
    assert again.status_code == 200
    with y.active():
        assert len([a for a in alerts_store.list_alerts("famB") if a.kind == "fed_moved"]) == 1

    # the old key can never advance anything: a higher epoch, or other content, is refused
    for forged in (
        moved_body(new, epoch=new.epoch + 1),
        moved_body(new, host="evil.example.org"),
        moved_body(new, pk=keys.generate()[1]),
    ):
        r = signed_call(z, "famA", y, fed_b, "POST", path, forged, private_key_b64=old.privateKey)
        assert r.status_code == 403, forged
    # the current key: older and equal-but-different are stale; a higher epoch is accepted
    for stale in (moved_body(new, epoch=new.epoch - 1), moved_body(new, host="other.example.org")):
        r = signed_call(z, "famA", y, fed_b, "POST", path, stale, private_key_b64=new.privateKey)
        assert r.status_code == 409, stale
    with y.active():
        link = fed_links.get(f"famB_{new.fedId}")
        assert (link.remote.host, link.remote.epoch) == ("relay-z.example.org", new.epoch)
    # malformed, and a fedId that is not the sender's
    r = signed_call(z, "famA", y, fed_b, "POST", path, {"nope": 1}, private_key_b64=new.privateKey)
    assert r.status_code == 400
    r = signed_call(
        z, "famA", y, fed_b, "POST", path, moved_body(new, epoch=9) | {"fedId": fed_b}, private_key_b64=new.privateKey
    )
    assert r.status_code == 400
    assert sign  # imported for the module's vocabulary


def test_the_link_stays_dormant_until_the_target_server_switch_is_on(world, net):
    x, y, z, _alias = world
    z.enable_server(False)
    result = move(x, z)
    assert any("OFF on this server" in n for n in result.report.notes)
    fed_b = identity_of(y, "famB").fedId
    with z.active():
        fed_sender.tick_pass()
        assert fed_links.get_for("famA", fed_b).announce_pending  # nothing sent
    assert not [h for h in net.hits if h.path == "/fed/v1/moved"]
    z.enable_server(True)
    with z.active():
        fed_sender.tick_pass()
        assert not fed_links.get_for("famA", fed_b).announce_pending


def test_a_failed_announcement_backs_off_and_is_retried_without_limit(world, net):
    x, y, z, _alias = world
    move(x, z)
    fed_b = identity_of(y, "famB").fedId
    net.down.add("relay-y.example.org")
    with z.active():
        import time

        fed_sender.tick_pass()
        link = fed_links.get_for("famA", fed_b)
        assert link.announce.attempts == 1 and link.announce.nextAt > time.time()
        # not due yet: nothing sent
        n = len(net.hits)
        fed_sender.tick_pass()
        assert len(net.hits) == n
        # due again after the backoff; after three tries it is due on every tick
        for i in range(1, 5):
            fed_sender.flush_link(link.id, now=time.time() + 1e6 * i)
        assert fed_links.get_for("famA", fed_b).announce.attempts == 5
        assert fed_families.get("famA").prevPrivateKey is not None  # still needed
    net.down.discard("relay-y.example.org")
    with z.active():
        fed_sender.tick_pass(now=__import__("time").time() + 1e8)
        assert fed_families.get("famA").prevPrivateKey is None


def test_a_refusal_is_asked_again_daily(world, net):
    x, y, z, _alias = world
    move(x, z)
    fed_b = identity_of(y, "famB").fedId
    with y.active():
        fed_links.delete(f"famB_{identity_of(x, 'famA').fedId}")  # Y no longer knows the family
    with z.active():
        import time

        fed_sender.tick_pass()
        a = fed_links.get_for("famA", fed_b).announce
        assert a.pending and a.nextAt - time.time() > 23 * 3600


def test_the_family_flag_is_preserved_and_the_report_says_which_switches(world):
    x, _y, z, _alias = world
    result = move(x, z)
    with z.active():
        from app.store import fed_state

        assert fed_state.family_flag("famA") is True
    assert any("told the new address" in n for n in result.report.notes)


def test_a_target_that_already_has_that_fed_id_refuses(world):
    x, _y, z, _alias = world
    result = move(x, z)
    assert result.report.ok
    with x.active():
        from app.store import families as families_store

        families_store.set_frozen("famA", True)
    data = export_bytes(x)
    with z.active():
        report = familyexport.plan_import(
            z.db, KpfamFile(io.BytesIO(data), PASS), ImportOptions(as_id="famCopy", target_kind="home")
        )
    assert not report.ok and any(c.kind == "fed" for c in report.collisions)
