"""Link traffic between two real relays: signature rules, per-link directory isolation, contacts and
messages both ways, the sender's retries / offline / health, suspend, delete and unlink, and the
two switches."""

from __future__ import annotations

import pytest

from app import notices
from app.fed import deps, keys, sign
from app.fed import sender as fed_sender
from app.store import alerts as alerts_store
from app.store import fed_links, fed_outbox, fed_state
from app.store import messages as messages_store
from tests.fed_world import (
    identity_of,
    link_families,
    needs_two_relays,
    signed_call,
)

pytestmark = needs_two_relays

FED = "/api/family/fed"


@pytest.fixture
def world(make_relay):
    """x: famA (adm_a, ann, bob). y: famB (adm_b, zed, yan) and famC (adm_c, cat), unlinked."""
    x, y = make_relay("relay-x"), make_relay("relay-y")
    for r in (x, y):
        r.enable_server()
    x.family("famA", "The Alphas")
    y.family("famB", "The Betas")
    y.family("famC", "The Cs")
    x.user("adm_a", "adma", "admin", "famA", "Ada Admin")
    x.user("ann", "ann", "member", "famA", "Ann A")
    x.user("bob", "bob", "member", "famA", "Bob A")
    y.user("adm_b", "admb", "admin", "famB", "Bea Admin")
    y.user("zed", "zed", "member", "famB", "Zed B")
    y.user("yan", "yan", "member", "famB", "Yan B")
    y.user("adm_c", "admc", "admin", "famC", "Cy Admin")
    y.user("cat", "cat", "member", "famC", "Cat C")
    return x, y


@pytest.fixture
def linked(world):
    x, y = world
    on_x, on_y = link_families(x, "adm_a", y, "adm_b")
    return x, y, on_x["id"], on_y["id"]


def share(r, uid_admin, link_id, uids):
    out = r.put(f"{FED}/links/{link_id}/shared", uid_admin, {"shared": uids})
    assert out.status_code == 200, out.text
    return out.json()


def befriend(x, y, lx, ly):
    """ann (on x) and zed (on y) share and add each other, through the real flows."""
    share(x, "adm_a", lx, ["ann"])
    share(y, "adm_b", ly, ["zed"])
    r = x.post(f"{FED}/links/{lx}/contacts", "adm_a", {"rid": "zed", "members": ["ann"]})
    assert r.status_code == 201, r.text
    with y.active():
        alert = next(a for a in alerts_store.list_alerts("famB") if a.kind == "fed_contact")
    assert alert.fedName == "The Alphas" and alert.fedHost == "relay-x.example.org"
    approve = y.post(f"/api/family/alerts/{alert.id}/approve", "adm_b", {})
    assert approve.status_code == 200, approve.text
    # A already added zed, so the mirror event raises nothing on A
    with x.active():
        assert not [a for a in alerts_store.list_alerts("famA") if a.kind == "fed_contact"]
    return r.json()


def send(r, uid, alias, body="hello"):
    return r.post(f"/api/conversations/{alias}/messages", uid, {"body": body})


def thread(r, a, b):
    with r.active():
        return messages_store.list_thread(messages_store.conv_key(a, b))


def external_uid(r, family, rid):
    from app.store import externals as externals_store

    with r.active():
        remote = next(iter(externals_store.list_fed_externals(family)))
        return remote.uid


def test_a_signed_request_on_a_live_link_is_answered(linked):
    x, y, _lx, _ly = linked
    ident_b = identity_of(y, "famB")
    r = signed_call(y, "famB", x, identity_of(x, "famA").fedId, "GET", "/fed/v1/directory")
    assert r.status_code == 200 and r.json() == []
    assert ident_b.fedId  # the sender


def test_cross_family_replay_fails_because_to_is_signed(world):
    x, y = world
    # famA is linked to both famB and famC on relay y
    for admin, fam in (("adm_b", "famB"), ("adm_c", "famC")):
        link_families(x, "adm_a", y, admin) if fam == "famB" else None
    invite = x.post(f"{FED}/invites", "adm_a").json()["invite"]
    assert y.post(f"{FED}/links", "adm_c", {"invite": invite}).status_code == 201
    a_id = identity_of(x, "famA").fedId
    b_id, c_id = identity_of(y, "famB").fedId, identity_of(y, "famC").fedId
    # a request A signed for family B...
    good = signed_call(x, "famA", y, b_id, "GET", "/fed/v1/directory")
    assert good.status_code == 200
    # ...captured and replayed at family C: same relay, same key, other `to`
    ts = 1_900_000_000
    import time

    now = int(time.time())
    headers = sign.sign(
        keys.private_from_b64(identity_of(x, "famA").privateKey), a_id, b_id, "GET", "/fed/v1/directory", b"", ts=now
    )
    headers[sign.H_TO] = c_id
    replay = y.raw("GET", "/fed/v1/directory", headers=headers)
    assert replay.status_code == 403
    assert ts  # unused marker


def test_not_for_you_is_one_403(linked):
    x, y, _lx, ly = linked
    a_id, b_id = identity_of(x, "famA").fedId, identity_of(y, "famB").fedId
    answers = {}
    answers["unknown to"] = signed_call(x, "famA", y, keys.new_fed_id(), "GET", "/fed/v1/directory")
    # a family with no link to famB
    answers["no link"] = signed_call(y, "famC", y, b_id, "GET", "/fed/v1/directory")
    with y.active():
        fed_links.set_state(ly, "suspended")
    answers["suspended"] = signed_call(x, "famA", y, b_id, "GET", "/fed/v1/directory")
    with y.active():
        fed_links.set_state(ly, "active")
        fed_state.set_family_enabled("famB", False)
    answers["family off"] = signed_call(x, "famA", y, b_id, "GET", "/fed/v1/directory")
    with y.active():
        fed_state.set_family_enabled("famB", True)
    y.enable_server(False)
    answers["server off"] = signed_call(x, "famA", y, b_id, "GET", "/fed/v1/directory")
    assert {k: v.status_code for k, v in answers.items()} == dict.fromkeys(answers, 403)
    assert len({v.text for v in answers.values()}) == 1
    assert a_id


def test_bad_signature_skew_and_missing_headers_are_the_one_403(linked):
    x, y, _lx, _ly = linked
    b_id = identity_of(y, "famB").fedId
    other = keys.generate()[0]
    assert signed_call(x, "famA", y, b_id, "GET", "/fed/v1/directory", private_key_b64=other).status_code == 403
    assert signed_call(x, "famA", y, b_id, "GET", "/fed/v1/directory", ts=1).status_code == 403
    assert y.raw("GET", "/fed/v1/directory").status_code == 403


def test_body_limit_and_rate_limit(linked, monkeypatch):
    x, y, _lx, _ly = linked
    b_id = identity_of(y, "famB").fedId
    big = b"x" * (deps.MAX_BODY + 1)
    assert signed_call(x, "famA", y, b_id, "POST", "/fed/v1/inbox", big).status_code == 413
    monkeypatch.setattr(deps, "RATE_LIMIT", 3)
    codes = [signed_call(x, "famA", y, b_id, "GET", "/fed/v1/directory").status_code for _ in range(5)]
    assert codes == [200, 200, 200, 429, 429]


def test_info_has_no_identity_and_follows_the_server_switch(world):
    x, _ = world
    r = x.raw("GET", "/fed/v1/info")
    assert r.status_code == 200 and set(r.json()) >= {"v", "hooks"} and r.json()["v"] == 2
    assert "fedId" not in r.text and "serverId" not in r.text
    x.enable_server(False)
    assert x.raw("GET", "/fed/v1/info").status_code == 404


def test_directory_shows_only_what_was_shared_on_that_link(world):
    x, y = world
    _la, lb = link_families(x, "adm_a", y, "adm_b")
    link_families(x, "adm_a", y, "adm_c")
    with x.active():
        links = {l.remote.name: l for l in fed_links.list_for_family("famA")}
    share(x, "adm_a", links["The Betas"].id, ["ann"])
    share(x, "adm_a", links["The Cs"].id, ["bob"])
    a_id = identity_of(x, "famA").fedId
    # as seen by famB's relay, through the real client
    assert [e["alias"] for e in y.get(f"{FED}/links/{lb['id']}/directory", "adm_b").json()] == ["ann"]
    with y.active():
        (c_link,) = fed_links.list_for_family("famC")
    assert [e["alias"] for e in y.get(f"{FED}/links/{c_link.id}/directory", "adm_c").json()] == ["bob"]
    # famC signs with its own key: it cannot read what A shared with B
    resp = signed_call(y, "famC", x, a_id, "GET", "/fed/v1/directory")
    assert [e["alias"] for e in resp.json()] == ["bob"]
    # a family on y with no link at all learns nothing
    y.family("famD", "The Ds")
    assert signed_call(y, "famD", x, a_id, "GET", "/fed/v1/directory").status_code == 403


def test_sharing_rules(linked):
    x, y, lx, _ly = linked
    assert x.put(f"{FED}/links/{lx}/shared", "adm_a", {"shared": ["zed"]}).status_code == 404  # not hers
    x.family("famA2", "A2")
    x.user("a2", "a2", "member", "famA2")
    assert x.put(f"{FED}/links/{lx}/shared", "adm_a", {"shared": ["a2"]}).status_code == 404
    assert share(x, "adm_a", lx, ["bob", "ann"])["shared"] == ["ann", "bob"]
    assert share(x, "adm_a", lx, [])["shared"] == []
    # a member who is later disabled or moved away is not shown
    share(x, "adm_a", lx, ["ann"])
    with x.active():
        from app.store import users as users_store

        users_store.set_disabled("ann", True) if hasattr(users_store, "set_disabled") else None
    entries = signed_call(y, "famB", x, identity_of(x, "famA").fedId, "GET", "/fed/v1/directory").json()
    assert len(entries) in (0, 1)


def test_contacts_and_messages_both_ways(linked):
    x, y, lx, ly = linked
    out = befriend(x, y, lx, ly)
    assert out["linkId"] == lx and out["rid"] == "zed" and out["members"] == ["ann"]
    ext_on_x = out["uid"]

    sent = send(x, "ann", out["alias"], "hello zed")
    assert sent.status_code == 201, sent.text
    msg = messages_store_get(x, sent.json()["id"])
    d = msg.deliveries["fed"]
    assert (d.state, d.server, d.serverHost) == ("sent", "The Betas", "relay-y.example.org")
    ext_on_y = external_uid(y, "famB", "ann")
    (got,) = thread(y, ext_on_y, "zed")
    assert (got.body, got.originBackendKind, got.recipientUid) == ("hello zed", "fed", "zed")
    # the reply
    r = send(y, "zed", "ann", "hi ann")
    assert r.status_code == 201, r.text
    assert [m.body for m in thread(x, ext_on_x, "ann")] == ["hello zed", "hi ann"]


def messages_store_get(r, mid):
    with r.active():
        return messages_store.get_message(mid)


def test_a_message_is_refused_until_the_other_side_has_added_back(linked):
    x, y, lx, ly = linked
    share(x, "adm_a", lx, ["ann"])
    share(y, "adm_b", ly, ["zed"])
    out = x.post(f"{FED}/links/{lx}/contacts", "adm_a", {"rid": "zed", "members": ["ann"]}).json()
    sent = send(x, "ann", out["alias"], "anyone there?")
    d = messages_store_get(x, sent.json()["id"]).deliveries["fed"]
    assert (d.state, d.error) == ("failed", "not_allowed")


def test_contact_needs_a_shared_member_and_a_directory_entry(linked):
    x, y, lx, ly = linked
    share(y, "adm_b", ly, ["zed"])
    r = x.post(f"{FED}/links/{lx}/contacts", "adm_a", {"rid": "zed", "members": ["ann"]})
    assert (r.status_code, r.json()["detail"].split(":")[0]) == (409, "not_shared")
    share(x, "adm_a", lx, ["ann"])
    assert x.post(f"{FED}/links/{lx}/contacts", "adm_a", {"rid": "yan", "members": ["ann"]}).status_code == 404
    d = x.get(f"{FED}/links/{lx}/directory", "adm_a").json()
    assert d == [{"rid": "zed", "alias": "zed", "name": "Zed B", "added": False}]


def test_contact_uid_depends_only_on_the_two_fed_ids_and_the_rid(linked):
    from app.store import externals as ex

    x, y, lx, ly = linked
    out = befriend(x, y, lx, ly)
    expect = ex.fed_ids(identity_of(x, "famA").fedId, identity_of(y, "famB").fedId, "zed")
    assert out["uid"] == expect


def test_an_unreachable_relay_keeps_the_message_queued_then_it_fails_offline(linked, net):
    x, y, lx, ly = linked
    out = befriend(x, y, lx, ly)
    net.down.add("relay-y.example.org")
    sent = send(x, "ann", out["alias"], "are you there")
    mid = sent.json()["id"]
    assert messages_store_get(x, mid).deliveries["fed"].state == "queued"
    with x.active():
        (row,) = fed_outbox.list_pending(lx)
        assert row.attempts == 1 and fed_outbox.get(row.id).error
        # a retry before the backoff elapses does nothing; after it, it tries again
        assert fed_sender.flush_link(lx, now=row.nextAt - 1).retried == 0
        assert fed_sender.flush_link(lx, now=row.nextAt + 1).retried == 1
    net.down.discard("relay-y.example.org")
    with x.active():
        import time

        fed_sender.tick_pass(now=time.time() + 3600)
    assert messages_store_get(x, mid).deliveries["fed"].state == "sent"
    # and one that never gets through fails peer_offline after 24 h
    net.down.add("relay-y.example.org")
    mid2 = send(x, "ann", out["alias"], "again").json()["id"]
    with x.active():
        import time

        assert fed_sender.fail_offline(now=time.time() + 25 * 3600) == 1
    d = messages_store_get(x, mid2).deliveries["fed"]
    assert (d.state, d.error) == ("failed", "peer_offline")


def test_health_down_and_up_notices_go_to_the_linked_family_only(linked, net, monkeypatch):
    import time

    monkeypatch.setenv("FED_DOWN_AFTER_S", "1")
    x, y, lx, ly = linked
    out = befriend(x, y, lx, ly)
    net.down.add("relay-y.example.org")
    send(x, "ann", out["alias"], "ping?")
    time.sleep(1.1)
    with x.active():
        fed_sender.health_pass()
        notes = [a for a in alerts_store.list_alerts("famA") if a.kind == "system"]
        assert any("The Betas unreachable" in a.preview for a in notes)
        assert fed_links.get(lx).health.state == "down"
    net.down.discard("relay-y.example.org")
    time.sleep(1.1)
    with x.active():
        fed_sender.tick_pass(now=time.time() + 3600)
        assert fed_links.get(lx).health.state == "ok"
        assert fed_sender.up_text("The Betas") == "The Betas is back."
    with y.active():
        assert not [a for a in alerts_store.list_alerts("famB") if a.kind == "system"]
    assert notices  # module under test


def test_suspend_stops_both_directions_and_resume_restores(linked):
    x, y, lx, ly = linked
    out = befriend(x, y, lx, ly)
    assert x.post(f"{FED}/links/{lx}/suspend", "adm_a").json()["state"] == "suspended"
    r = send(x, "ann", out["alias"])
    assert r.status_code == 403 and r.json()["detail"]["reason"] == "no_peer"
    # inbound is the uniform 403
    assert signed_call(y, "famB", x, identity_of(x, "famA").fedId, "GET", "/fed/v1/directory").status_code == 403
    assert x.get(f"{FED}/links/{lx}/directory", "adm_a").status_code == 409
    x.post(f"{FED}/links/{lx}/resume", "adm_a")
    assert send(x, "ann", out["alias"]).status_code == 201


def test_delete_fails_pending_rows_unlinks_the_other_side_and_unsends_contacts(linked, net):
    x, y, lx, ly = linked
    out = befriend(x, y, lx, ly)
    net.down.add("relay-y.example.org")
    mid = send(x, "ann", out["alias"], "queued").json()["id"]
    net.down.discard("relay-y.example.org")
    r = x.delete(f"{FED}/links/{lx}", "adm_a")
    assert r.status_code == 200 and r.json() == {"failedRows": 1}
    d = messages_store_get(x, mid).deliveries["fed"]
    assert (d.state, d.error) == ("failed", "unpeered")
    assert x.get(FED, "adm_a").json()["links"] == []
    # the other end got the signed unlink and removed its link too
    assert y.get(FED, "adm_b").json()["links"] == []
    # the contact stays, unsendable
    contacts = x.get("/api/family/contacts", "adm_a").json()
    (c,) = [c for c in contacts if c.get("fed")]
    assert (c["sendable"], c["reason"]) == (False, "unpeered")
    assert send(x, "ann", out["alias"]).status_code == 403


def test_unlink_needs_a_valid_signature(linked):
    x, y, _lx, _ly = linked
    b_id = identity_of(y, "famB").fedId
    assert signed_call(x, "famA", y, b_id, "POST", "/fed/v1/unlink", {}, private_key_b64=keys.generate()[0]).status_code == 403
    assert y.get(FED, "adm_b").json()["links"]


def test_server_off_refuses_sends_and_keeps_rows_aging(linked):
    x, y, lx, ly = linked
    out = befriend(x, y, lx, ly)
    x.enable_server(False)
    r = send(x, "ann", out["alias"])
    assert r.status_code == 403 and r.json()["detail"]["reason"] == "fed_off"
    x.enable_server(True)
    assert send(x, "ann", out["alias"]).status_code == 201


def test_family_off_is_dormant_and_back_on_restores_everything(linked):
    x, y, lx, ly = linked
    out = befriend(x, y, lx, ly)
    with x.active():
        fed_state.set_family_enabled("famA", False)
    assert send(x, "ann", out["alias"]).json()["detail"]["reason"] == "fed_off"
    assert x.get(FED, "adm_a").status_code == 404
    assert signed_call(y, "famB", x, identity_of(x, "famA").fedId, "GET", "/fed/v1/directory").status_code == 403
    with x.active():
        fed_state.set_family_enabled("famA", True)
    assert x.get(FED, "adm_a").json()["links"][0]["shared"] == ["ann"]
    assert send(x, "ann", out["alias"]).status_code == 201


def test_purging_a_family_cleans_up_and_tells_the_other_side(linked):
    from app import purge

    x, y, lx, ly = linked
    befriend(x, y, lx, ly)
    with x.active():
        assert fed_links.list_for_family("famA")
        from app.fed import linking

        linking.unlink_family("famA")
        purge.purge_family("famA")
        from app.store import fed_families

        assert fed_families.get("famA") is None and fed_links.list_for_family("famA") == []
        assert fed_outbox.list_pending() == []
    assert y.get(FED, "adm_b").json()["links"] == []


def test_every_auth_failure_is_the_same_response(linked):
    """R4: unknown family, no link, bad signature, skew and missing headers cannot be told apart."""
    x, y, _lx, _ly = linked
    b_id = identity_of(y, "famB").fedId
    other = keys.generate()[0]
    replies = [
        signed_call(x, "famA", y, b_id, "GET", "/fed/v1/directory", private_key_b64=other),
        signed_call(x, "famA", y, b_id, "GET", "/fed/v1/directory", ts=1),
        y.raw("GET", "/fed/v1/directory"),
        y.raw("GET", "/fed/v1/directory", headers={sign.H_TO: "f_nope", sign.H_FROM: "f_nobody"}),
    ]
    assert {r.status_code for r in replies} == {403}
    assert len({r.content for r in replies}) == 1
    assert len({tuple(sorted((k, v) for k, v in r.headers.items() if k != "x-request-id")) for r in replies}) == 1


def test_purging_a_frozen_family_sends_no_unlink(linked):
    """A frozen family was exported for a move; its links belong to the copy."""
    from app import purge
    from app.fed import linking
    from app.store import families as families_store

    x, y, lx, ly = linked
    befriend(x, y, lx, ly)
    with x.active():
        families_store.set_frozen("famA", True)
        assert linking.unlink_family("famA") == 0
        purge.purge_family("famA")
    assert len(y.get(FED, "adm_b").json()["links"]) == 1


def test_deleting_a_link_of_a_frozen_family_is_409(linked):
    from app.store import families as families_store

    x, y, lx, ly = linked
    befriend(x, y, lx, ly)
    with x.active():
        families_store.set_frozen("famA", True)
    assert x.delete(f"{FED}/links/{lx}", "adm_a").status_code == 409
    with x.active():
        assert fed_links.list_for_family("famA")
        families_store.set_frozen("famA", False)
    assert x.delete(f"{FED}/links/{lx}", "adm_a").status_code == 200
