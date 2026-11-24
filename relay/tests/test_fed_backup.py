"""Backup and reauthorization.

X is the live family's relay, Y a family it is linked to, Z where the backup is restored (the
fedId is the family's, so a restore next to the original on one server is refused)."""

from __future__ import annotations

import io
import json
import time

import pytest

from app import familyexport
from app.familyexport import ExportOptions, ImportOptions, KpfamFile
from app.fed import keys, sign
from app.fed import sender as fed_sender
from app.fed.linking import decode_invite
from app.store import alerts as alerts_store
from app.store import fed_families, fed_links
from app.store import users as users_store
from tests.fed_world import (
    identity_of,
    link_families,
    needs_two_relays,
    signed_call,
)

pytestmark = needs_two_relays

FED = "/api/family/fed"
WEB = "https://web-y.example.org"


@pytest.fixture
def world(make_relay):
    x = make_relay("relay-x")
    y = make_relay("relay-y", web_base_url=WEB)
    z = make_relay("relay-z")
    for r in (x, y, z):
        r.enable_server()
    x.family("famA", "The Alphas")
    y.family("famB", "The Betas")
    y.family("famC", "The Cs")
    z.family("famRoot", "Root", federated=False)
    x.user("adm_a", "adma", "admin", "famA", "Ada Admin")
    x.user("ann", "ann", "member", "famA", "Ann A")
    y.user("adm_b", "admb", "admin", "famB", "Bea Admin")
    y.user("zed", "zed", "member", "famB", "Zed B")
    y.user("adm_c", "admc", "admin", "famC")
    z.user("root", "root", "super", "famRoot")
    lx, ly = link_families(x, "adm_a", y, "adm_b")
    x.put(f"{FED}/links/{lx['id']}/shared", "adm_a", {"shared": ["ann"]})
    y.put(f"{FED}/links/{ly['id']}/shared", "adm_b", {"shared": ["zed"]})
    out = x.post(f"{FED}/links/{lx['id']}/contacts", "adm_a", {"rid": "zed", "members": ["ann"]}).json()
    with y.active():
        alert = next(a for a in alerts_store.list_alerts("famB") if a.kind == "fed_contact")
    y.post(f"/api/family/alerts/{alert.id}/approve", "adm_b", {})
    return x, y, z, out["alias"]


def backup_bytes(relay, fid="famA", passphrase=None) -> bytes:
    buf = io.BytesIO()
    with relay.active():
        familyexport.write_kpfam(
            relay.db, fid, buf, passphrase=passphrase, opts=ExportOptions(backup=True)
        )
    return buf.getvalue()


def restore(z, data, passphrase=None, **kw):
    with z.active():
        result = familyexport.apply_import(
            z.db, KpfamFile(io.BytesIO(data), passphrase), ImportOptions(target_kind="home", **kw)
        )
    z.sign_in("adm_a", "ann")
    return result


@pytest.fixture
def restored(world):
    x, y, z, alias = world
    result = restore(z, backup_bytes(x))
    return x, y, z, alias, result


def y_link(y, x):
    return f"famB_{identity_of(x, 'famA').fedId}"


def test_a_backup_of_a_live_family_holds_no_private_key(world):
    x, _y, _z, _alias = world
    ident = identity_of(x, "famA")
    # a backup needs no frozen family and no passphrase
    data = backup_bytes(x)
    f = KpfamFile(io.BytesIO(data), None)
    assert f.manifest().kind == "backup"
    text = b"".join(f.lines()).decode()
    assert ident.privateKey not in text and "privateKey" not in text and "prevPrivateKey" not in text
    rows = {r["path"]: r["data"] for r in f.rows()}
    fam = rows["fedFamilies/famA"]
    assert (fam["fedId"], fam["epoch"], fam["publicKey"]) == (ident.fedId, ident.epoch, ident.publicKey)
    (link_path,) = [p for p in rows if p.startswith("fedLinks/")]
    link = rows[link_path]
    assert link["shared"] == ["ann"] and link["remote"]["host"] == "relay-y.example.org"
    assert link["remote"]["publicKey"] and link["remote"]["fedId"]
    assert rows["families/famA"]["federation"] is True
    assert any(r.get("fed") for p, r in rows.items() if p.startswith("users/") and p.count("/") == 1)


def test_an_encrypted_backup_and_the_http_route(world):
    x, _y, _z, _alias = world
    x.family("famS", "S")
    x.user("root_x", "rootx", "super", "famS")
    enc = backup_bytes(x, passphrase="pw")
    assert enc.startswith(familyexport.MAGIC)
    with x.active():
        for headers in ({}, {"X-Export-Passphrase": "pw"}):
            resp = x.http.get(
                "/api/admin/families/famA/backup", headers={**x.auth("root_x"), **headers}
            )
            assert resp.status_code == 200
            f = KpfamFile(io.BytesIO(resp.content), "pw" if headers else None)
            assert f.manifest().kind == "backup"
        # a family admin may not
        assert x.http.get("/api/admin/families/famA/backup", headers=x.auth("adm_a")).status_code == 403
        assert x.http.get("/api/admin/families/nope/backup", headers=x.auth("root_x")).status_code == 404
        # the move route still wants a frozen family and a passphrase
        assert x.http.get(
            "/api/admin/families/famA/export",
            headers={**x.auth("root_x"), "X-Export-Passphrase": "pw"},
        ).status_code == 409


def test_restore_keeps_the_fed_id_makes_a_new_key_and_parks_every_link(restored):
    x, _y, z, _alias, result = restored
    ident = identity_of(x, "famA")
    assert result.report.ok and result.report.kind == "backup"
    assert result.report.federation.model_dump() == {"links": 1, "reauth": 1, "announcing": 0}
    assert any("reauthoriz" in n for n in result.report.notes)
    with z.active():
        new = fed_families.get("famA")
        assert (new.fedId, new.epoch) == (ident.fedId, ident.epoch + 1)
        assert new.publicKey != ident.publicKey and new.privateKey != ident.privateKey
        assert new.prevPrivateKey is None
        (link,) = fed_links.list_for_family("famA")
        assert link.state == "reauth" and link.shared == ["ann"] and link.announce is None
        assert not link.remoteKeyChanged
        # the contact (a federated external with its backend) came back too
        assert [u for u in users_store.list_users() if u.ownerFamilyId == "famA" and u.fed]


def test_a_reauth_link_sends_nothing_accepts_nothing_and_can_be_deleted(restored, net):
    x, y, z, alias, _ = restored
    # outbound: refused at the gate with its own reason, and the book says so
    sent = z.post(f"/api/conversations/{alias}/messages", "ann", {"body": "hi"})
    assert sent.status_code == 403 and sent.json()["detail"]["reason"] == "fed_reauth"
    contacts = z.get("/api/family/contacts", "adm_a").json()
    (c,) = [c for c in contacts if c.get("fed")]
    assert (c["sendable"], c["reason"]) == (False, "fed_reauth")
    # inbound: the uniform 403 (the other family signs with the key it has for us: the old one)
    ident = identity_of(x, "famA")
    r = signed_call(y, "famB", z, ident.fedId, "GET", "/fed/v1/directory")
    assert r.status_code == 403
    seen = len(net.hits)
    with z.active():
        fed_sender.tick_pass()
        fed_sender.ping_pass(now=time.time() + 1e6)
    assert net.hits[seen:] == []  # not a single request left Z
    # suspend / resume cannot be used to switch it on
    (lnk,) = z.get(FED, "adm_a").json()["links"]
    assert lnk["state"] == "reauth" and lnk["remoteKeyChanged"] is False
    for action in ("suspend", "resume"):
        assert z.post(f"{FED}/links/{lnk['id']}/{action}", "adm_a").status_code == 409
    # the other relays' directory and contacts calls are refused too
    assert z.get(f"{FED}/links/{lnk['id']}/directory", "adm_a").status_code == 409
    # an unwanted reauth link goes with the normal delete
    assert z.delete(f"{FED}/links/{lnk['id']}", "adm_a").status_code == 200
    assert z.get(FED, "adm_a").json()["links"] == []


def reauth_invite(z) -> dict:
    (lnk,) = z.get(FED, "adm_a").json()["links"]
    out = z.post(f"{FED}/links/{lnk['id']}/reauth", "adm_a")
    assert out.status_code == 201, out.text
    return out.json()


def test_the_reauth_invite_and_its_url(restored):
    x, y, z, _alias, _ = restored
    out = reauth_invite(z)
    assert set(out) == {"invite", "url", "expiresAt"}
    blob = decode_invite(out["invite"])
    ident = identity_of(x, "famA")
    with z.active():
        new = fed_families.get("famA")
    assert blob.re == 1 and blob.fid == ident.fedId and blob.pk == new.publicKey
    assert blob.host == "relay-z.example.org" and blob.name == "The Alphas" and blob.epoch == new.epoch
    # the other relay reported its web base: the invite rides in the fragment
    assert out["url"] == f"{WEB}/family/people#fedInvite={out['invite']}"
    assert "?" not in out["url"].split("#")[0]
    # a link that needs no reauthorization has nothing to reauthorize
    (lnk,) = y.get(FED, "adm_b").json()["links"]
    assert y.post(f"{FED}/links/{lnk['id']}/reauth", "adm_b").status_code == 409


def test_the_reauth_url_follows_the_other_relays_reported_web_base(world):
    x, y, z, _alias = world
    assert y.raw("GET", "/fed/v1/info").json() == {"v": 2, "hooks": False, "web": WEB}
    assert x.raw("GET", "/fed/v1/info").json()["web"] is None
    # restore on Z; its link points at Y, which has a web base: make Y's report null
    restore(z, backup_bytes(x))
    import dataclasses

    y.app.state.settings = dataclasses.replace(y.settings, web_base_url=None)
    assert reauth_invite(z)["url"] is None


def test_reauth_needs_the_other_admin_to_confirm_then_both_sides_are_active(restored, net):
    x, y, z, alias, _ = restored
    ident_old = identity_of(x, "famA")
    out = reauth_invite(z)
    with z.active():
        new = fed_families.get("famA")
    lid = y_link(y, x)

    # first call: nothing changes, the admin is shown what is about to happen
    r = y.post(f"{FED}/links", "adm_b", {"invite": out["invite"]})
    assert r.status_code == 409
    d = r.json()["detail"]
    assert d["code"] == "reauth_confirm"
    assert d["newFingerprint"] == keys.fingerprint(new.publicKey)
    assert (d["newHost"], d["newPort"]) == ("relay-z.example.org", 443)
    assert d["link"]["id"] == lid and d["link"]["state"] == "active"
    assert d["link"]["remote"]["host"] == "relay-x.example.org"
    with y.active():
        assert fed_links.get(lid).remote.publicKey == ident_old.publicKey

    # confirm
    r = y.post(f"{FED}/links", "adm_b", {"invite": out["invite"], "confirmReauth": True})
    assert r.status_code == 201, r.text
    got = r.json()
    assert got["id"] == lid and got["state"] == "active" and got["remoteKeyChanged"] is False
    assert got["remote"]["host"] == "relay-z.example.org"
    assert got["remote"]["fingerprint"] == keys.fingerprint(new.publicKey)
    assert got["shared"] == ["zed"]  # untouched
    with y.active():
        link = fed_links.get(lid)
        assert (link.remote.publicKey, link.remote.epoch, link.state) == (new.publicKey, new.epoch, "active")
        alert = next(a for a in alerts_store.list_alerts("famB") if a.kind == "fed_reauth")
        assert alert.fedName == "The Alphas" and "restored from a backup" in alert.preview
        assert "relay-z.example.org" in alert.preview
    with z.active():
        (zl,) = fed_links.list_for_family("famA")
        assert zl.state == "active" and zl.shared == ["ann"] and not zl.remoteKeyChanged

    # traffic flows, both ways, contacts intact; the old key is dead
    assert y.post("/api/conversations/ann/messages", "zed", {"body": "welcome back"}).status_code == 201
    assert z.post(f"/api/conversations/{alias}/messages", "ann", {"body": "thanks"}).status_code == 201
    fed_b = identity_of(y, "famB").fedId
    assert signed_call(x, "famA", y, fed_b, "GET", "/fed/v1/directory").status_code == 403
    assert signed_call(z, "famA", y, fed_b, "GET", "/fed/v1/directory").status_code == 200


    # the original (still alive) family is visibly cut off: Y no longer accepts its key
    # (asserted above) and holds the restored family's key for the link
def test_a_re_invite_without_a_link_or_with_a_stale_epoch_is_refused(restored):
    x, y, z, _alias, _ = restored
    out = reauth_invite(z)
    # famC on Y has no link to that family: a `re` invite is not a way to make one
    r = y.post(f"{FED}/links", "adm_c", {"invite": out["invite"], "confirmReauth": True})
    assert r.status_code == 400
    with y.active():
        assert fed_links.list_for_family("famC") == []
    # and a normal invite for a family already linked stays "already linked"
    inv = x.post(f"{FED}/invites", "adm_a").json()["invite"]
    assert y.post(f"{FED}/links", "adm_b", {"invite": inv}).status_code == 409

    out = reauth_invite(z)
    with y.active():
        fed_links.update_remote(y_link(y, x), epoch=99)
    r = y.post(f"{FED}/links", "adm_b", {"invite": out["invite"], "confirmReauth": True})
    assert r.status_code == 400


def test_reauth_code_expires_and_works_once(restored):
    _x, y, z, _alias, _ = restored
    out = reauth_invite(z)
    with z.active():
        from app.db.firestore import get_db

        for snap in get_db().collection("fedInvites").stream():
            snap.reference.update({"expiresAt": 1})
    r = y.post(f"{FED}/links", "adm_b", {"invite": out["invite"], "confirmReauth": True})
    assert (r.status_code, r.json()["detail"]) == (502, "refused")
    with z.active():
        assert fed_links.list_for_family("famA")[0].state == "reauth"

    # single use: a fresh code, used; someone else holding Y's family id cannot use it again
    out = reauth_invite(z)
    assert y.post(f"{FED}/links", "adm_b", {"invite": out["invite"], "confirmReauth": True}).status_code == 201
    blob = decode_invite(out["invite"])
    stranger_private, stranger_public = keys.generate()
    fed_b = identity_of(y, "famB").fedId
    body = {
        "code": blob.code,
        "to": blob.fid,
        "re": 1,
        "from": {"fedId": fed_b, "pk": stranger_public, "name": "X", "host": "relay-y.example.org", "port": 443, "epoch": 5},
    }
    raw = json.dumps(body).encode()
    headers = sign.sign(keys.private_from_b64(stranger_private), fed_b, blob.fid, "POST", "/fed/v1/link", raw)
    headers["Content-Type"] = "application/json"
    assert z.raw("POST", "/fed/v1/link", content=raw, headers=headers).status_code == 403


def test_a_changed_remote_key_is_accepted_on_the_code_and_flagged(restored):
    _x, y, z, _alias, _ = restored
    out = reauth_invite(z)
    # the backup's stored key for Y is not the one Y signs with now (Y moved since the backup)
    with z.active():
        (zl,) = fed_links.list_for_family("famA")
        fed_links.update_remote(zl.id, publicKey=keys.generate()[1])
    r = y.post(f"{FED}/links", "adm_b", {"invite": out["invite"], "confirmReauth": True})
    assert r.status_code == 201
    with z.active():
        (zl,) = fed_links.list_for_family("famA")
        assert zl.state == "active" and zl.remoteKeyChanged is True
        assert zl.remote.publicKey == identity_of(y, "famB").publicKey
    (lnk,) = z.get(FED, "adm_a").json()["links"]
    assert lnk["remoteKeyChanged"] is True


def test_a_mismatched_signature_in_a_reauth_request_is_refused(restored):
    _x, y, z, _alias, _ = restored
    out = reauth_invite(z)
    blob = decode_invite(out["invite"])
    fed_b = identity_of(y, "famB").fedId
    pk = identity_of(y, "famB").publicKey
    body = {
        "code": blob.code,
        "to": blob.fid,
        "re": 1,
        "from": {"fedId": fed_b, "pk": pk, "name": "B", "host": "relay-y.example.org", "port": 443},
    }
    raw = json.dumps(body).encode()
    other = keys.generate()[0]  # not the key it claims: no proof of possession
    headers = sign.sign(keys.private_from_b64(other), fed_b, blob.fid, "POST", "/fed/v1/link", raw)
    headers["Content-Type"] = "application/json"
    assert z.raw("POST", "/fed/v1/link", content=raw, headers=headers).status_code == 401
    with z.active():
        assert fed_links.list_for_family("famA")[0].state == "reauth"


def test_the_target_switches_still_decide(world):
    x, _y, z, _alias = world
    z.enable_server(False)
    restore(z, backup_bytes(x))
    assert z.get(FED, "adm_a").status_code == 404
    z.enable_server(True)
    assert z.get(FED, "adm_a").json()["links"][0]["state"] == "reauth"
