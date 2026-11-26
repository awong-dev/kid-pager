"""Family identity and linking two families.

Real relays in one process (`tests/fed_world.py`): the invite is made on one, pasted on the other,
and the two exchange real signed HTTP through both apps."""

from __future__ import annotations

import json

import pytest

from app.fed import keys, sign
from app.store import fed_families, fed_links
from tests.fed_world import (
    identity_of,
    link_families,
    needs_two_relays,
)

pytestmark = needs_two_relays

FED = "/api/family/fed"


@pytest.fixture
def world(make_relay):
    x, y = make_relay("relay-x"), make_relay("relay-y")
    for r in (x, y):
        r.enable_server()
    x.family("famA", "The Alphas")
    y.family("famB", "The Betas")
    x.user("adm_a", "adma", "admin", "famA", "Ada Admin")
    x.user("kid_a", "kida", "member", "famA", "Kid A")
    y.user("adm_b", "admb", "admin", "famB", "Bea Admin")
    y.user("kid_b", "kidb", "member", "famB", "Kid B")
    return x, y


def test_identity_is_created_on_first_call_and_is_stable(world):
    x, _ = world
    with x.active():
        assert fed_families.get("famA") is None
    first = x.get(FED, "adm_a")
    assert first.status_code == 200
    body = first.json()
    assert keys.FED_ID_RE.match(body["fedId"]) and body["name"] == "The Alphas"
    assert body["links"] == []
    ident = identity_of(x, "famA")
    assert body["fingerprint"] == keys.fingerprint(ident.publicKey)
    assert x.get(FED, "adm_a").json()["fedId"] == body["fedId"]
    # the private key never leaves the relay
    assert ident.privateKey not in first.text


def test_family_routes_are_for_the_family_admin_only(world):
    x, _ = world
    assert x.get(FED, "kid_a").status_code == 403
    assert x.post(f"{FED}/invites", "kid_a").status_code == 403


def test_invite_then_link_makes_a_link_on_both_sides(world, net):
    x, y = world
    on_x, on_y = link_families(x, "adm_a", y, "adm_b")
    ident_a, ident_b = identity_of(x, "famA"), identity_of(y, "famB")

    assert on_y["remote"] == {
        "fedId": ident_a.fedId,
        "name": "The Alphas",
        "host": "relay-x.example.org",
        "port": 443,
        "path": "",
        "fingerprint": keys.fingerprint(ident_a.publicKey),
    }
    assert on_x["remote"] == {
        "fedId": ident_b.fedId,
        "name": "The Betas",
        "host": "relay-y.example.org",
        "port": 443,
        "path": "",
        "fingerprint": keys.fingerprint(ident_b.publicKey),
    }
    for out in (on_x, on_y):
        assert out["state"] == "active" and out["shared"] == []
        assert out["health"] == {"state": "ok", "since": 0}
        assert (out["pending"], out["announcePending"]) == (0, False)
        assert out["createdAt"] is not None
    # both sides listed it, under the family's own identity
    assert x.get(FED, "adm_a").json()["links"][0]["id"] == f"famA_{ident_b.fedId}"
    assert y.get(FED, "adm_b").json()["links"][0]["id"] == f"famB_{ident_a.fedId}"
    # the acceptor's request really was signed and addressed to the issuer's family
    (hit,) = [h for h in net.hits if h.path == "/fed/v1/link"]
    assert hit.host_header == "relay-x.example.org" and hit.sni == "relay-x.example.org"
    assert hit.headers["x-kp-fed-to"] == ident_a.fedId
    assert hit.headers["x-kp-fed-from"] == ident_b.fedId and hit.status == 200


def test_the_invite_blob_describes_the_issuer(world):
    from app.fed.linking import decode_invite

    x, _ = world
    out = x.post(f"{FED}/invites", "adm_a")
    assert out.status_code == 201
    blob = decode_invite(out.json()["invite"])
    ident = identity_of(x, "famA")
    assert (blob.host, blob.port, blob.fid, blob.pk, blob.name) == (
        "relay-x.example.org",
        443,
        ident.fedId,
        ident.publicKey,
        "The Alphas",
    )
    assert out.json()["invite"].startswith("kpfed3.")
    assert out.json()["expiresAt"] > 0 and blob.ca is None


def test_a_private_ca_rides_in_the_invite_and_is_stored_as_the_links_only_trust_root(
    make_relay, net
):
    from tests.test_fed_client import make_ca, pem

    _, cert, _ = make_ca()
    x = make_relay("relay-x", fed_ca_pem=pem(cert))
    y = make_relay("relay-y")
    for r in (x, y):
        r.enable_server()
    x.family("famA")
    y.family("famB")
    x.user("adm_a", "adma", "admin", "famA")
    y.user("adm_b", "admb", "admin", "famB")
    link_families(x, "adm_a", y, "adm_b")
    with y.active():
        (link,) = fed_links.list_for_family("famB")
    assert link.remote.tlsCa == pem(cert)


def test_a_code_works_once(world):
    x, y = world
    y.family("famC", "The Cs")
    y.user("adm_c", "admc", "admin", "famC")
    invite = x.post(f"{FED}/invites", "adm_a").json()["invite"]
    assert y.post(f"{FED}/links", "adm_b", {"invite": invite}).status_code == 201
    again = y.post(f"{FED}/links", "adm_c", {"invite": invite})
    assert (again.status_code, again.json()["detail"]) == (502, "refused")
    with x.active():
        assert len(fed_links.list_for_family("famA")) == 1


def test_an_expired_or_wrong_or_other_familys_code_is_refused(world):
    from app.db.firestore import get_db
    from app.fed.linking import InviteBlob, decode_invite, encode_invite

    x, y = world
    invite = x.post(f"{FED}/invites", "adm_a").json()["invite"]
    with x.active():
        for snap in get_db().collection("fedInvites").stream():
            snap.reference.update({"expiresAt": 1})
    resp = y.post(f"{FED}/links", "adm_b", {"invite": invite})
    assert (resp.status_code, resp.json()["detail"]) == (502, "refused")
    with y.active():
        assert fed_links.list_for_family("famB") == []

    x.family("famA2", "Other Alphas")
    x.user("adm_a2", "adma2", "admin", "famA2")
    blob_a = decode_invite(x.post(f"{FED}/invites", "adm_a").json()["invite"])
    blob_a2 = decode_invite(x.post(f"{FED}/invites", "adm_a2").json()["invite"])
    # family A's code, aimed at family A2's identity
    mixed = encode_invite(InviteBlob(**{**blob_a2.model_dump(), "code": blob_a.code}))
    resp = y.post(f"{FED}/links", "adm_b", {"invite": mixed})
    assert (resp.status_code, resp.json()["detail"]) == (502, "refused")

    forged = encode_invite(InviteBlob(**{**blob_a.model_dump(), "code": "A" * 22}))
    resp = y.post(f"{FED}/links", "adm_b", {"invite": forged})
    assert (resp.status_code, resp.json()["detail"]) == (502, "refused")
    with x.active():
        assert fed_links.list_for_family("famA") == []
    # the real code still works afterwards: a wrong guess does not burn it
    real = encode_invite(blob_a)
    assert y.post(f"{FED}/links", "adm_b", {"invite": real}).status_code == 201


def test_linking_to_yourself_is_refused(world):
    x, _ = world
    invite = x.post(f"{FED}/invites", "adm_a").json()["invite"]
    resp = x.post(f"{FED}/links", "adm_a", {"invite": invite})
    assert resp.status_code == 409
    with x.active():
        assert fed_links.list_for_family("famA") == []


def test_two_families_of_one_relay_can_link(world):
    x, _ = world
    x.family("famA2", "Other Alphas")
    x.user("adm_a2", "adma2", "admin", "famA2")
    link_families(x, "adm_a", x, "adm_a2")
    assert len(x.get(FED, "adm_a").json()["links"]) == 1
    assert len(x.get(FED, "adm_a2").json()["links"]) == 1


def test_already_linked_is_a_409_from_either_side_and_when_only_the_issuer_still_has_it(world):
    x, y = world
    link_families(x, "adm_a", y, "adm_b")
    invite_a = x.post(f"{FED}/invites", "adm_a").json()["invite"]
    assert y.post(f"{FED}/links", "adm_b", {"invite": invite_a}).status_code == 409
    invite_b = y.post(f"{FED}/invites", "adm_b").json()["invite"]
    assert x.post(f"{FED}/links", "adm_a", {"invite": invite_b}).status_code == 409

    # B forgets the link (its row removed locally); A still has it: A answers 409.
    with y.active():
        (link,) = fed_links.list_for_family("famB")
        fed_links.delete(link.id)
    invite = x.post(f"{FED}/invites", "adm_a").json()["invite"]
    assert y.post(f"{FED}/links", "adm_b", {"invite": invite}).status_code == 409


def test_a_family_has_at_most_16_links(world):
    x, y = world
    with y.active():
        for _ in range(16):
            fed_links.create(
                "famB",
                remote=fed_links.Remote(
                    fedId=keys.new_fed_id(), name="n", publicKey=keys.generate()[1], host="h.example.org"
                ),
                created_by=None,
            )
    # the acceptor at the limit
    invite = x.post(f"{FED}/invites", "adm_a").json()["invite"]
    assert y.post(f"{FED}/links", "adm_b", {"invite": invite}).status_code == 409
    # the issuer at the limit: the code survives the refusal
    x.family("famA9", "Nine")
    x.user("adm_a9", "adma9", "admin", "famA9")
    resp = x.post(f"{FED}/links", "adm_a9", {"invite": y.post(f"{FED}/invites", "adm_b").json()["invite"]})
    assert resp.status_code == 409


def test_a_malformed_invite_is_a_400(world):
    x, _ = world
    for bad in ("", "nonsense", "kpfed1.abcd", "kpfed3.!!!", "kpfed3." + "A" * 20):
        resp = x.post(f"{FED}/links", "adm_a", {"invite": bad or " "})
        assert resp.status_code in (400, 422), bad
    assert x.post(f"{FED}/links", "adm_a", {"invite": "kpfed3.e30"}).status_code == 400


def test_link_transport_errors_are_reported_without_the_other_relays_words(world, net):
    x, y = world
    host = "relay-x.example.org"
    invite = x.post(f"{FED}/invites", "adm_a").json()["invite"]

    net.down.add(host)
    resp = y.post(f"{FED}/links", "adm_b", {"invite": invite})
    assert (resp.status_code, resp.json()["detail"]) == (502, "unreachable")
    net.down.discard(host)

    net.dns[host] = ["169.254.169.254"]
    resp = y.post(f"{FED}/links", "adm_b", {"invite": invite})
    assert (resp.status_code, resp.json()["detail"]) == (502, "address_not_allowed")
    assert not [h for h in net.hits if h.ip == "169.254.169.254"]  # nothing was sent
    del net.dns[host]

    net.refuse[host] = (500, {"detail": "secret internals"})
    resp = y.post(f"{FED}/links", "adm_b", {"invite": invite})
    assert (resp.status_code, resp.json()["detail"]) == (502, "refused")
    assert "secret" not in resp.text  # the other side's words are never shown
    net.refuse[host] = (200, {"fedId": "x"})
    resp = y.post(f"{FED}/links", "adm_b", {"invite": invite})
    assert (resp.status_code, resp.json()["detail"]) == (502, "bad_response")


def test_a_confirmation_signed_by_another_key_is_refused(world, net):
    """The invite's `pk` is what the admins vouched for: a relay answering with another key
    (a swapped invite or a man in the middle) does not get linked."""
    from app.fed.linking import InviteBlob, decode_invite, encode_invite

    x, y = world
    blob = decode_invite(x.post(f"{FED}/invites", "adm_a").json()["invite"])
    swapped = encode_invite(InviteBlob(**{**blob.model_dump(), "pk": keys.generate()[1]}))
    resp = y.post(f"{FED}/links", "adm_b", {"invite": swapped})
    assert (resp.status_code, resp.json()["detail"]) == (502, "bad_response")
    with y.active():
        assert fed_links.list_for_family("famB") == []


def link_request(x, y, *, code, to, who, signer=None, ts=None, headers_from=None):
    """`who`: `(fedId, private, public)` of an acceptor that need not exist anywhere."""
    fed_id, private, public = who
    body = {
        "code": code,
        "to": to,
        "from": {
            "fedId": fed_id,
            "pk": public,
            "name": "Mallory",
            "host": "relay-y.example.org",
            "port": 443,
            "path": "",
        },
    }
    raw = json.dumps(body).encode()
    headers = sign.sign(
        keys.private_from_b64(signer or private), headers_from or fed_id, to, "POST", "/fed/v1/link", raw, ts=ts
    )
    headers["Content-Type"] = "application/json"
    return x.raw("POST", "/fed/v1/link", content=raw, headers=headers)


def stranger():
    private, public = keys.generate()
    return keys.new_fed_id(), private, public


def issue(x, uid="adm_a"):
    from app.fed.linking import decode_invite

    blob = decode_invite(x.post(f"{FED}/invites", uid).json()["invite"])
    return blob


def test_the_link_request_proves_possession_of_the_key_in_its_body(world):
    x, _ = world
    blob = issue(x)
    who = stranger()
    other_private = keys.generate()[0]
    # signed with a key other than from.pk: a claim of someone else's key
    resp = link_request(x, x, code=blob.code, to=blob.fid, who=who, signer=other_private)
    assert resp.status_code == 401
    with x.active():
        assert fed_links.list_for_family("famA") == []
    # skewed timestamp
    resp = link_request(x, x, code=blob.code, to=blob.fid, who=who, ts=1)
    assert resp.status_code == 401
    # headers that disagree with the body
    resp = link_request(x, x, code=blob.code, to=blob.fid, who=who, headers_from=keys.new_fed_id())
    assert resp.status_code == 400
    # and the right way works, once
    assert link_request(x, x, code=blob.code, to=blob.fid, who=who).status_code == 200
    with x.active():
        (link,) = fed_links.list_for_family("famA")
    assert link.remote.fedId == who[0] and link.remote.publicKey == who[2]


def test_the_confirmation_is_signed_for_the_acceptor_and_is_not_a_request(world):
    x, _ = world
    blob = issue(x)
    who = stranger()
    resp = link_request(x, x, code=blob.code, to=blob.fid, who=who)
    parsed = sign.parse_headers(resp.headers)
    assert (parsed.sender, parsed.receiver) == (blob.fid, who[0])
    sign.verify(keys.public_from_b64(blob.pk), parsed, sign.RESPONSE_METHOD, "/fed/v1/link", resp.content)
    with pytest.raises(sign.SignatureError):
        sign.verify(keys.public_from_b64(blob.pk), parsed, "POST", "/fed/v1/link", resp.content)


def test_every_not_for_you_is_the_same_403(world):
    """Unknown family, a family switched off, the server switched off, a bad code: one answer."""
    x, _y = world
    blob = issue(x)
    who = stranger()
    answers = []
    # a code that is wrong
    answers.append(link_request(x, x, code="Z" * 22, to=blob.fid, who=who))
    # a family nobody has
    answers.append(link_request(x, x, code=blob.code, to=keys.new_fed_id(), who=who))
    # the family switched off
    with x.active():
        from app.store import fed_state

        fed_state.set_family_enabled("famA", False)
    answers.append(link_request(x, x, code=blob.code, to=blob.fid, who=who))
    with x.active():
        fed_state.set_family_enabled("famA", True)
    # the whole server off
    x.enable_server(False)
    answers.append(link_request(x, x, code=blob.code, to=blob.fid, who=who))
    assert {a.status_code for a in answers} == {403}
    assert len({a.text for a in answers}) == 1
    # nothing leaked into the stores
    with x.active():
        assert fed_links.list_for_family("famA") == []


def test_the_link_route_is_rate_limited_per_source(world):
    x, _ = world
    blob = issue(x)
    codes = []
    for _ in range(12):
        codes.append(link_request(x, x, code="Z" * 22, to=blob.fid, who=stranger()).status_code)
    assert codes[:10] == [403] * 10 and codes[10:] == [429, 429]


def test_everything_is_404_unless_the_family_is_effective(world):
    x, _ = world
    with x.active():
        from app.store import fed_state

        fed_state.set_family_enabled("famA", False)
    for method, path in (
        ("GET", FED),
        ("POST", f"{FED}/invites"),
        ("POST", f"{FED}/links"),
        ("PUT", f"{FED}/links/x/shared"),
        ("GET", f"{FED}/links/x/directory"),
    ):
        assert x.api(method, path, "adm_a", {} if method != "GET" else None).status_code == 404, path
    with x.active():
        fed_state.set_family_enabled("famA", True)
    x.enable_server(False)
    assert x.get(FED, "adm_a").status_code == 404


def test_a_super_lists_suspends_resumes_and_deletes_any_link(world):
    x, y = world
    link_families(x, "adm_a", y, "adm_b")
    x.family("famZ", "Zed")
    x.user("root", "root", "super", "famZ")
    links = x.get("/api/admin/fed/links", "root").json()
    assert len(links) == 1 and links[0]["familyId"] == "famA" and links[0]["familyName"] == "The Alphas"
    lid = links[0]["id"]
    assert x.post(f"/api/admin/fed/links/{lid}/suspend", "root").json()["state"] == "suspended"
    assert x.get(FED, "adm_a").json()["links"][0]["state"] == "suspended"
    assert x.post(f"/api/admin/fed/links/{lid}/resume", "root").json()["state"] == "active"
    assert x.delete(f"/api/admin/fed/links/{lid}", "root").status_code == 200
    assert x.get("/api/admin/fed/links", "root").json() == []
    assert x.post("/api/admin/fed/links/nope/suspend", "root").status_code == 404
    # a family admin has no part in the super surface
    assert x.get("/api/admin/fed/links", "adm_a").status_code == 403
    # the other end was told (the unlink): its link is gone too
    assert y.get(FED, "adm_b").json()["links"] == []


def test_a_family_admin_cannot_touch_another_familys_link(world):
    x, y = world
    link_families(x, "adm_a", y, "adm_b")
    x.family("famA2", "Other Alphas")
    x.user("adm_a2", "adma2", "admin", "famA2")
    (lnk,) = x.get(FED, "adm_a").json()["links"]
    for method, suffix, body in (
        ("POST", "/suspend", None),
        ("POST", "/resume", None),
        ("PUT", "/shared", {"shared": []}),
        ("GET", "/directory", None),
        ("DELETE", "", None),
    ):
        resp = x.api(method, f"{FED}/links/{lnk['id']}{suffix}", "adm_a2", body)
        assert resp.status_code == 404, suffix
