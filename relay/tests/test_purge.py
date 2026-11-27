"""Registry-driven cascading deletes (`app/purge.py`)."""

from __future__ import annotations

from datetime import UTC, datetime

from app import datamap, purge
from app.db.firestore import get_db

NOW = datetime(2026, 11, 1, tzinfo=UTC)


class FakeBrokerAdmin:
    def __init__(self, result: str = "ok", raises: bool = False) -> None:
        self.deleted: list[str] = []
        self._result = result
        self._raises = raises

    def delete_user(self, username: str) -> str:
        self.deleted.append(username)
        if self._raises:
            raise RuntimeError("broker down")
        return self._result


def _put(path: str, data: dict) -> None:
    parts = path.split("/")
    ref = get_db().collection(parts[0]).document(parts[1])
    for i in range(2, len(parts), 2):
        ref = ref.collection(parts[i]).document(parts[i + 1])
    ref.set(data)


def _exists(path: str) -> bool:
    parts = path.split("/")
    ref = get_db().collection(parts[0]).document(parts[1])
    for i in range(2, len(parts), 2):
        ref = ref.collection(parts[i]).document(parts[i + 1])
    return ref.get().exists


def _seed(fid: str) -> dict[str, str]:
    """One document in every non-global registry row, all belonging to family `fid`."""
    u1, u2, ext = f"{fid}-u1", f"{fid}-u2", f"{fid}-ext"
    dev, br = f"{fid}-dev", f"{fid}-br"
    _put(f"families/{fid}", {"name": fid})
    _put(f"families/{fid}/alerts/a1", {"status": "open", "subjectUid": u1, "ts": NOW})
    for uid, alias in ((u1, f"{fid}one"), (u2, f"{fid}two")):
        _put(
            f"users/{uid}",
            {"familyId": fid, "alias": alias, "smsNumber": f"+1555{uid}"},
        )
        _put(f"aliases/{alias}", {"uid": uid})
        _put(f"smsNumbers/+1555{uid}", {"uid": uid})
        _put(f"users/{uid}/credentials/c1", {"k": 1})
        _put(f"users/{uid}/backends/b1", {"kind": "webapp"})
        _put(f"users/{uid}/book/x", {"k": 1})
        _put(f"users/{uid}/pushSubscriptions/p1", {"k": 1})
        _put(f"users/{uid}/pushTokens/t1", {"k": 1})
        _put(f"sessions/s-{uid}", {"uid": uid, "lastSeenAt": NOW})
        _put(f"webauthnChallenges/w-{uid}", {"uid": uid, "expiresAt": NOW})
        _put(f"inviteCodes/i-{uid}", {"uid": uid, "createdBy": uid, "expiresAt": NOW})
        _put(f"moveClaims/{uid}", {"uid": uid, "moveId": "mv_0123456789ab"})
        _put(f"gchatLinkCodes/g-{uid}", {"uid": uid, "expiresAt": 1})
        _put(f"gchatSpaces/sp-{uid}", {"uid": uid})
    _put(f"users/{ext}", {"ownerFamilyId": fid, "kind": "external", "alias": f"x{fid}"})
    _put(f"users/{u1}/backends/pager1", {"kind": "pager", "config": {"deviceId": dev}})
    _put(f"allow/{u1}_{u2}", {"fromUid": u1, "toUid": u2, "familyIds": [fid]})
    _put(f"contactNames/{fid}_abc", {"uid": ext, "familyId": fid})
    _put(
        f"devices/{dev}", {"familyId": fid, "ownerUid": u1, "mqttUsername": f"mq-{dev}"}
    )
    for sub in ("locations", "battery", "smsLog"):
        _put(f"devices/{dev}/{sub}/r1", {"createdAt": NOW, "peerUid": u2})
    _put(f"deviceSecrets/{dev}", {"hmacKey": "x"})
    _put(f"locReqs/{dev}", {"createdAt": NOW})
    _put(f"locWireIds/{dev}_1", {"deviceId": dev, "createdAt": NOW})
    _put(f"setupCodes/sc-{fid}", {"deviceId": dev, "expiresAt": NOW})
    _put(
        f"contactRequests/{dev}_1", {"deviceId": dev, "ownerUid": u1, "createdAt": NOW}
    )
    conv = f"{u1}_{u2}"
    _put(
        f"messages/m-{fid}",
        {
            "kind": "text",
            "uids": [u1, u2],
            "senderUid": u1,
            "recipientUid": u2,
            "wireId": f"w{fid}",
            "familyIds": [fid],
            "pendingDeviceIds": [dev],
            "createdAt": NOW,
        },
    )
    _put(f"wireIds/w{fid}_{u2}", {"messageId": f"m-{fid}", "createdAt": NOW})
    _put(
        f"conversations/{conv}",
        {"uids": [u1, u2], "familyIds": [fid], "lastMessageAt": NOW},
    )
    # a family's federation identity, link, invite and outbox row
    _put(f"fedFamilies/{fid}", {"familyId": fid, "fedId": f"f_{fid}", "privateKey": "x"})
    _put(
        f"fedLinks/{fid}_f_remote",
        {
            "familyId": fid,
            "remote": {"fedId": "f_remote", "name": "R", "publicKey": "k", "host": "r.example"},
            "shared": [u1],
        },
    )
    _put(f"fedInvites/inv-{fid}", {"familyId": fid, "expiresAt": NOW})
    _put(f"fedOutbox/ob-{fid}", {"familyId": fid, "linkId": f"{fid}_f_remote", "state": "pending"})
    _put(f"heldSms/h-{fid}", {"familyId": fid, "toUid": u1, "receivedAt": NOW})
    _put(f"heldChat/hc-{fid}", {"familyId": fid, "toUid": u1, "receivedAt": NOW})
    _put(f"bridges/{br}", {"familyId": fid, "ownerUid": u1})
    _put(f"bridges/{br}/outbox/o1", {"ackedAt": NOW})
    _put(f"bridges/{br}/topups/t1", {"at": NOW, "by": u1, "pack": "text100", "state": "confirmed"})
    _put(f"bridges/{br}/carrierLog/c1", {"at": NOW, "dir": "in", "text": "x"})
    _put(f"bridgePairCodes/pc-{fid}", {"bridgeId": br, "expiresAt": NOW})
    _put(
        f"bridgeConversations/{br}_1", {"bridgeId": br, "ownerUid": u1, "familyId": fid}
    )
    return {"u1": u1, "u2": u2, "ext": ext, "dev": dev, "br": br}


def _dump() -> dict[str, dict]:
    """Every document of every registered collection (parents and children alike)."""
    db = get_db()
    out: dict[str, dict] = {}
    for coll in datamap.REGISTRY:
        if coll.name == "settings":
            continue
        base = (
            db.collection_group(coll.name) if coll.parent else db.collection(coll.name)
        )
        for s in base.stream():
            out[s.reference.path] = s.to_dict() or {}
    return out


def _scoped_to(dump: dict[str, dict], fid: str, owners: dict[str, str]) -> list[str]:
    ids = set(owners.values()) | {fid}
    hits = []
    for path, data in dump.items():
        if ids & set(path.split("/")):
            hits.append(path)
            continue
        if any(
            f in (fid,) for f in (data.get("familyId"), data.get("ownerFamilyId"))
        ) or fid in (data.get("familyIds") or []):
            hits.append(path)
            continue
        coll_name = path.split("/")[-2]
        for fname in datamap.BY_NAME[coll_name].uid_fields:
            val = data.get(fname)
            vals = val if isinstance(val, list) else [val]
            if ids & {v for v in vals if isinstance(v, str)}:
                hits.append(path)
                break
    return hits


def test_seed_touches_every_non_global_registry_row():
    _seed("a")
    present = {p.split("/")[-2] for p in _dump()}
    wanted = {c.name for c in datamap.REGISTRY if c.family != "global"}
    assert wanted <= present, f"seed a document for: {sorted(wanted - present)}"


def test_purge_family_removes_everything_scoped_to_it_and_leaves_the_other_family_identical():
    owners_b = _seed("b")
    before = _dump()
    broker = FakeBrokerAdmin()

    owners_a = _seed("a")
    assert _scoped_to(_dump(), "a", owners_a)  # the seed is really there

    report = purge.purge_family("a", broker)

    after = _dump()
    assert _scoped_to(after, "a", owners_a) == []
    assert after == before
    assert not any("families/a" in p for p in after)
    assert broker.deleted == ["mq-a-dev"]
    assert report.deleted["users"] == 3  # two members and the external
    assert report.deleted["alerts"] == 1
    assert owners_b["dev"] not in broker.deleted


def test_purge_device_removes_its_rows_and_keeps_the_owner():
    owners = _seed("a")
    broker = FakeBrokerAdmin()
    report = purge.purge_device(owners["dev"], broker)

    dev = owners["dev"]
    assert not _exists(f"devices/{dev}")
    for sub in ("locations", "battery", "smsLog"):
        assert not _exists(f"devices/{dev}/{sub}/r1")
    assert not _exists(f"deviceSecrets/{dev}")
    assert not _exists(f"locReqs/{dev}")
    assert not _exists(f"locWireIds/{dev}_1")
    assert not _exists("setupCodes/sc-a")
    assert not _exists(f"contactRequests/{dev}_1")
    assert not _exists(f"users/{owners['u1']}/backends/pager1")
    assert _exists(
        f"users/{owners['u1']}/backends/b1"
    )  # the owner's other backends stay
    assert _exists(f"users/{owners['u1']}")
    msg = get_db().collection("messages").document("m-a").get().to_dict()
    assert msg["pendingDeviceIds"] == []
    assert broker.deleted == ["mq-a-dev"]
    assert report.broker == {dev: "ok"}


def test_purge_device_tolerates_an_absent_broker_user_and_a_broker_failure():
    owners = _seed("a")
    assert purge.purge_device(owners["dev"], FakeBrokerAdmin("ok")).broker == {
        owners["dev"]: "ok"
    }
    owners = _seed("a")
    down = FakeBrokerAdmin(raises=True)
    report = purge.purge_device(owners["dev"], down)  # does not raise
    assert report.broker == {owners["dev"]: "error"}
    assert not _exists(f"devices/{owners['dev']}")


def test_purge_device_of_an_already_deleted_device_still_clears_leftovers():
    owners = _seed("a")
    get_db().collection("devices").document(owners["dev"]).delete()
    broker = FakeBrokerAdmin()
    purge.purge_device(owners["dev"], broker)
    assert not _exists(f"deviceSecrets/{owners['dev']}")
    assert broker.deleted == [
        owners["dev"]
    ]  # no doc to read the username from: id by convention


def test_purge_user_cascades_but_not_to_the_rest_of_the_family():
    owners = _seed("a")
    purge.purge_user(owners["u1"], FakeBrokerAdmin())
    u1, u2 = owners["u1"], owners["u2"]
    assert not _exists(f"users/{u1}")
    assert not _exists(f"users/{u1}/credentials/c1")
    assert not _exists("aliases/aone")
    assert not _exists(f"smsNumbers/+1555{u1}")
    assert not _exists(f"sessions/s-{u1}")
    assert not _exists(f"allow/{u1}_{u2}")
    assert not _exists("messages/m-a")
    assert not _exists(f"wireIds/wa_{u2}")
    assert not _exists(f"conversations/{u1}_{u2}")
    assert not _exists(f"devices/{owners['dev']}")  # owned device
    assert not _exists(f"bridges/{owners['br']}")
    assert not _exists("families/a/alerts/a1")  # names the user
    assert _exists(f"users/{u2}")
    assert _exists("aliases/atwo")
    assert _exists("families/a")


def test_group_conversation_only_loses_the_member():
    _put(
        "conversations/grp",
        {"uids": ["g1", "g2", "g3"], "participants": {"g1": {}, "g2": {}, "g3": {}}},
    )
    _put("users/g1", {"familyId": "gf"})
    purge.purge_user("g1")
    data = get_db().collection("conversations").document("grp").get().to_dict()
    assert data["uids"] == ["g2", "g3"]
    assert "g1" not in data["participants"]


def test_shared_message_between_families_is_deleted_with_the_purged_user():
    _put("users/p1", {"familyId": "fa"})
    _put("users/q1", {"familyId": "fb"})
    _put(
        "messages/mx",
        {
            "uids": ["p1", "q1"],
            "senderUid": "p1",
            "recipientUid": "q1",
            "familyIds": ["fa", "fb"],
        },
    )
    purge.purge_family("fa")
    assert not _exists("messages/mx")
    assert _exists("users/q1")


def test_is_frozen_reads_a_plain_boolean():
    _put("families/ff", {"name": "x"})
    assert not purge.is_frozen("ff")
    _put("families/ff", {"name": "x", "frozen": True})
    assert purge.is_frozen("ff")
    assert not purge.is_frozen("nope")
