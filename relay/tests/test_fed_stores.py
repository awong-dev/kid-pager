"""`fedFamilies`, `fedLinks`, `fedInvites`, `fedOutbox` stores."""

from __future__ import annotations

import pytest

from app.fed import keys
from app.store import fed_families, fed_links, fed_outbox

A = "famA"
R1 = "f_aaaaaaaaaaaaaaaaaaaa"
R2 = "f_bbbbbbbbbbbbbbbbbbbb"


def remote(fed_id=R1, **kw) -> fed_links.Remote:
    return fed_links.Remote(
        fedId=fed_id, name=kw.pop("name", "Rem"), publicKey=keys.generate()[1], host="r.example.org", **kw
    )


def test_identity_is_created_once_and_is_stable():
    first = fed_families.get_or_create(A)
    assert keys.FED_ID_RE.match(first.fedId) and first.epoch == 0 and first.prevPrivateKey is None
    assert keys.public_of(first.privateKey) == first.publicKey
    again = fed_families.get_or_create(A)
    assert again == first
    assert fed_families.get_by_fed_id(first.fedId).familyId == A
    assert fed_families.get_by_fed_id(R1) is None
    other = fed_families.get_or_create("famB")
    assert other.fedId != first.fedId and other.publicKey != first.publicKey


def test_rotation_is_pure_and_keeps_the_old_private_key():
    row = fed_families.get_or_create(A).model_dump()
    out = fed_families.rotated(row)
    assert out["fedId"] == row["fedId"] and out["epoch"] == 1
    assert out["prevPrivateKey"] == row["privateKey"]
    assert out["privateKey"] != row["privateKey"] and keys.public_of(out["privateKey"]) == out["publicKey"]
    assert fed_families.rotated(out)["epoch"] == 2
    fed_families.clear_prev(A)  # nothing to clear, no error
    assert fed_families.get(A).prevPrivateKey is None


def test_link_defaults_and_id():
    link = fed_links.create(A, remote=remote(), created_by="u1")
    assert link.id == f"{A}_{R1}" == fed_links.link_id(A, R1)
    assert (link.state, link.shared, link.announce) == ("active", [], None)
    assert link.health.state == "ok" and not link.announce_pending
    assert fed_links.get_for(A, R1) == link
    assert fed_links.get_for("famB", R1) is None  # one family's link is not another's


def test_create_is_create_only_and_limited_to_16():
    fed_links.create(A, remote=remote(), created_by=None)
    with pytest.raises(fed_links.LinkExists):
        fed_links.create(A, remote=remote(), created_by=None)
    for _ in range(15):
        fed_links.create(A, remote=remote(keys.new_fed_id()), created_by=None)
    assert len(fed_links.list_for_family(A)) == 16
    with pytest.raises(fed_links.LimitReached):
        fed_links.create(A, remote=remote(R2), created_by=None)
    # another family has its own 16
    fed_links.create("famB", remote=remote(R2), created_by=None)


def test_state_shared_health_and_delete():
    link = fed_links.create(A, remote=remote(), created_by=None)
    assert fed_links.set_state(link.id, "suspended").state == "suspended"
    assert fed_links.set_shared(link.id, ["u2", "u1", "u2"]).shared == ["u1", "u2"]
    fed_links.record_ok(link.id)
    fed_links.record_err(link.id)
    fed_links.set_health(link.id, state="down", since=5, notifiedAt=6)
    got = fed_links.get(link.id)
    assert got.lastOkAt and got.lastErrAt and got.health.model_dump() == {
        "state": "down",
        "since": 5,
        "notifiedAt": 6,
    }
    fed_links.delete(link.id)
    assert fed_links.get(link.id) is None
    for call in (fed_links.set_state, fed_links.set_shared):
        with pytest.raises(KeyError):
            call(link.id, "active" if call is fed_links.set_state else [])


def test_remove_shared_uid_touches_only_links_that_share_it():
    a = fed_links.create(A, remote=remote(R1), created_by=None)
    b = fed_links.create("famB", remote=remote(R2), created_by=None)
    fed_links.set_shared(a.id, ["u1", "u2"])
    fed_links.set_shared(b.id, ["u3"])
    assert fed_links.remove_shared_uid("u1") == 1
    assert fed_links.get(a.id).shared == ["u2"] and fed_links.get(b.id).shared == ["u3"]


def test_update_remote_and_announce():
    link = fed_links.create(A, remote=remote(), created_by=None)
    fed_links.update_remote(link.id, epoch=3, host="new.example.org", prevPublicKey="old")
    got = fed_links.get(link.id)
    assert (got.remote.epoch, got.remote.host, got.remote.prevPublicKey) == (3, "new.example.org", "old")
    fed_links.set_announce(link.id, fed_links.Announce(pending=True, since=1))
    assert fed_links.get(link.id).announce_pending
    assert [x.id for x in fed_links.pending_announce(A)] == [link.id]
    fed_links.set_announce(link.id, None)
    assert fed_links.pending_announce(A) == []
    row = fed_links.announce_pending_data({"familyId": A}, now=7)
    assert row["announce"] == {"pending": True, "since": 7, "attempts": 0, "nextAt": 0}


DAY = 24 * 3600


@pytest.mark.parametrize(
    "steps",
    [
        pytest.param([("code", A, 1000, "ok"), ("code", A, 1000, "used")], id="single_use"),
        pytest.param([("nope", A, 1000, "unknown")], id="unknown"),
        pytest.param([("code", A, 1000 + DAY + 1, "expired"), ("code", A, 1000 + DAY, "ok")], id="expires"),
        pytest.param([("code", "famB", 1000, "wrong_family"), ("code", A, 1000, "ok")], id="belongs_to_its_family"),
    ],
)
def test_consume_invite_outcomes(steps):
    fed_links.create_invite("code", A, "u1", now=1000)
    consumed = False
    for code, family, now, expected in steps:
        assert fed_links.consume_invite(code, family, used_by=R1, used_pk="pk", now=now) == expected
        consumed = consumed or expected == "ok"
        if code == "code":
            inv = fed_links.get_invite(code)
            assert (inv["usedAt"] is not None) == consumed  # an expired code is left for the sweep
            if consumed:
                assert (inv["usedBy"], inv["usedPk"]) == (R1, "pk")
    from app.db.firestore import get_db

    ids = [s.id for s in get_db().collection("fedInvites").stream()]
    assert ids == [fed_links.code_hash("code")] and "code" not in ids


def test_enqueue_is_idempotent_and_pending():
    row = fed_outbox.enqueue("ob_1", f"{A}_{R1}", A, {"type": "ping", "id": "p"}, now=10)
    assert (row.state, row.attempts, row.nextAt, row.linkId, row.familyId) == (
        "pending",
        0,
        10,
        f"{A}_{R1}",
        A,
    )
    again = fed_outbox.enqueue("ob_1", "other", "famZ", {"type": "ping", "id": "q"}, now=99)
    assert again.event["id"] == "p" and again.linkId == f"{A}_{R1}"


def test_pending_in_order_per_link_and_due_batches():
    for i in range(3):
        fed_outbox.enqueue(f"ob_a{i}", "L1", A, {"id": str(i)}, now=100)
    fed_outbox.enqueue("ob_b", "L2", A, {"id": "b"}, now=100)
    assert [r.id for r in fed_outbox.list_pending("L1")] == ["ob_a0", "ob_a1", "ob_a2"]
    assert len(fed_outbox.list_pending()) == 4
    assert [r.id for r in fed_outbox.due_batch("L1", now=100, limit=2)] == ["ob_a0", "ob_a1"]
    fed_outbox.record_attempt("ob_a0", 500, "boom")
    assert fed_outbox.due_batch("L1", now=200) == []  # the oldest backs off and holds the rest
    assert fed_outbox.get("ob_a0").attempts == 1 and fed_outbox.get("ob_a0").error == "boom"
    assert len(fed_outbox.due_batch("L1", now=500)) == 3


def test_ack_fail_and_fail_for_link():
    for i in range(3):
        fed_outbox.enqueue(f"ob_{i}", "L1", A, {"id": str(i)}, msg_id=f"m{i}", now=1)
    fed_outbox.mark_acked("ob_0")
    fed_outbox.mark_failed("ob_1", "policy")
    assert (fed_outbox.get("ob_0").state, fed_outbox.get("ob_1").error) == ("acked", "policy")
    failed = fed_outbox.fail_pending_for_link("L1", "unpeered")
    assert [r.id for r in failed] == ["ob_2"] and fed_outbox.get("ob_2").error == "unpeered"
    assert fed_outbox.list_pending() == []
