"""A two-family database as raw documents, for the family export/import tests.
Family `fa` is moved; every identifier of the neighbour family `fb` contains `zzb`."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from app import dbexport
from app.db import sqlitestore

TS = {"$ts": 1793448000000000}
TS2 = {"$ts": 1793448060000000}

FA = "fa"
FB = "fzzb"

KID, MOM, SUP = "u_kida", "u_moma", "u_supa"
EXT = "x_exta"
NUM_KID = "+12065550101"
NUM_EXT = "+12065550111"
DEV = "pgr-a1"
BR = "b_aaaa"
GROUP = "g_a1"
BKID, BMOM, BEXT = "u_kidzzb", "u_momzzb", "x_extzzb"
BNUM, BNUM_EXT = "+12065550999", "+12065550888"
BDEV = "pgr-zzb1"
BBR = "b_zzbb"
B_STRINGS = ("zzb", BNUM, BNUM_EXT)


def _user(uid: str, alias: str, **kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "alias": alias,
        "displayName": alias.title(),
        "email": None,
        "phone": None,
        "role": "member",
        "familyId": None,
        "kind": "person",
        "ownerFamilyId": None,
        "policy": {"out": "people", "in": "people"},
        "notify": {},
        "disabled": False,
        "createdAt": TS,
        "smsNumber": None,
    }
    base.update(kw)
    return base


def _msg(
    seq: int,
    conv: str,
    uids: list[str],
    sender: str,
    rcpt: str,
    fams: list[str],
    **kw: Any,
):
    d: dict[str, Any] = {
        "seq": seq,
        "convKey": conv,
        "uids": uids,
        "senderUid": sender,
        "recipientUid": rcpt,
        "kind": "text",
        "body": f"hello {seq}",
        "loc": None,
        "wireId": f"w{seq}",
        "originBackendKind": "webapp",
        "originBackendId": None,
        "ts": 1793448000,
        "createdAt": TS,
        "deliveries": {},
        "pendingDeviceIds": [],
        "groupMsgId": None,
        "senderAlias": None,
        "familyIds": fams,
    }
    d.update(kw)
    return d


def world_records() -> list[dict[str, Any]]:
    r: list[dict[str, Any]] = []

    def add(path: str, data: dict[str, Any]) -> None:
        r.append({"path": path, "data": data})

    add(
        f"families/{FA}",
        {
            "name": "Alpha",
            "blockedNumbers": ["+12065550666"],
            "createdAt": TS,
            "frozen": True,
        },
    )
    add(
        f"families/{FB}",
        {"name": "Neighbour zzb", "blockedNumbers": [], "createdAt": TS},
    )
    add(
        f"families/{FA}/alerts/a1",
        {
            "kind": "sms_unknown",
            "status": "open",
            "subjectUid": KID,
            "peerUid": EXT,
            "ts": TS,
        },
    )
    add(
        f"families/{FA}/alerts/a2",
        {
            "kind": "new_conversation",
            "status": "open",
            "subjectUid": KID,
            "peerUid": BKID,
            "ts": TS,
        },
    )
    add(
        f"families/{FB}/alerts/b1",
        {"kind": "x", "status": "open", "subjectUid": BKID, "ts": TS},
    )

    add(
        f"users/{SUP}",
        _user(SUP, "supa", role="super", familyId=FA, email="sup@example.com"),
    )
    add(
        f"users/{MOM}",
        _user(MOM, "moma", role="admin", familyId=FA, email="mom@example.com"),
    )
    add(f"users/{KID}", _user(KID, "kida", familyId=FA, smsNumber=NUM_KID))
    add(
        f"users/{EXT}",
        _user(
            EXT,
            "xexta",
            kind="external",
            ownerFamilyId=FA,
            phone=NUM_EXT,
            displayName="Gran",
        ),
    )
    for uid, alias in ((SUP, "supa"), (MOM, "moma"), (KID, "kida"), (EXT, "xexta")):
        add(f"aliases/{alias}", {"uid": uid})
    add(f"smsNumbers/{NUM_KID}", {"uid": KID})
    add(
        f"users/{KID}/backends/bk1",
        {"kind": "pager", "config": {"deviceId": DEV}, "enabled": True},
    )
    add(
        f"users/{EXT}/backends/bsms",
        {"kind": "sms", "config": {"phone": NUM_EXT}, "enabled": True},
    )
    add(f"users/{KID}/book/{MOM}", {"nick": "Mum"})
    add(f"users/{KID}/book/{BKID}", {"nick": "Neighbour"})
    add(
        f"users/{MOM}/credentials/cred1",
        {"credentialId": {"$b64": "AAEC"}, "publicKey": {"$b64": "AwQF"}},
    )
    add(f"users/{MOM}/pushTokens/t1", {"token": "fcm-a"})
    add(f"users/{MOM}/pushSubscriptions/s1", {"endpoint": "https://push.example/a"})
    add(f"users/{BKID}", _user(BKID, "kidzzb", familyId=FB, smsNumber=BNUM))
    add(f"users/{BMOM}", _user(BMOM, "momzzb", role="admin", familyId=FB))
    add(
        f"users/{BEXT}",
        _user(BEXT, "xextzzb", kind="external", ownerFamilyId=FB, phone=BNUM_EXT),
    )
    for uid, alias in ((BKID, "kidzzb"), (BMOM, "momzzb"), (BEXT, "xextzzb")):
        add(f"aliases/{alias}", {"uid": uid})
    add(f"smsNumbers/{BNUM}", {"uid": BKID})
    add(f"users/{BKID}/book/{BMOM}", {"nick": "Mama"})

    def edge(a: str, b: str, fams: list[str]) -> None:
        add(
            f"allow/{a}_{b}",
            {
                "fromUid": a,
                "toUid": b,
                "message": True,
                "locate": False,
                "familyIds": fams,
            },
        )

    edge(KID, MOM, [FA])
    edge(MOM, KID, [FA])
    edge(KID, EXT, [FA])
    edge(KID, BKID, sorted([FA, FB]))
    edge(BKID, KID, sorted([FA, FB]))
    edge(BKID, BMOM, [FB])

    add(
        f"devices/{DEV}",
        {
            "ownerUid": KID,
            "label": "Kid pager",
            "familyId": FA,
            "defaultToUid": MOM,
            "locatableBy": [MOM, BKID],
            "revokedAt": None,
            "status": {"lat": 1.5},
            "authMode": "hmac",
            "provisionState": "provisioned",
            "bookVersion": 3,
            "mqttUsername": DEV,
            "pendingBook": {"v": 3},
        },
    )
    add(
        f"deviceSecrets/{DEV}",
        {"hmacKey": {"$b64": "a2V5"}, "upN": 7, "downN": 9, "mqttPasswordHash": "h"},
    )
    add(f"devices/{DEV}/locations/l1", {"lat": 1.5, "lon": 2.5, "createdAt": TS})
    add(f"devices/{DEV}/battery/b1", {"mv": 3900, "createdAt": TS})
    add(
        f"devices/{DEV}/smsLog/s1", {"peerUid": EXT, "body": "sms in", "receivedAt": TS}
    )
    add(
        f"devices/{DEV}/smsLog/s2", {"peerUid": BEXT, "body": "stray", "receivedAt": TS}
    )
    add(
        f"devices/{BDEV}",
        {
            "ownerUid": BKID,
            "label": "zzb",
            "familyId": FB,
            "locatableBy": [],
            "status": {},
        },
    )
    add(f"deviceSecrets/{BDEV}", {"hmacKey": {"$b64": "enpi"}, "upN": 1, "downN": 1})
    add(f"devices/{BDEV}/locations/l9", {"lat": 9.0, "lon": 9.0, "createdAt": TS})
    add(f"locReqs/{DEV}", {"createdAt": TS})
    add(f"locWireIds/{DEV}_x", {"createdAt": TS})
    add(
        f"setupCodes/{hashlib.sha256(b'c').hexdigest()}",
        {"deviceId": DEV, "expiresAt": TS2},
    )

    add(
        f"bridges/{BR}",
        {
            "ownerUid": KID,
            "familyId": FA,
            "label": "phone",
            "tokenHash": "th",
            "fcmToken": "fcm-x",
            "simNumber": NUM_KID,
            "lastSeenAt": TS,
            "caps": {"sms": True},
            "createdAt": TS,
        },
    )
    add(f"bridges/{BR}/outbox/o1", {"text": "x", "ackedAt": TS})
    add(
        f"bridges/{BBR}",
        {
            "ownerUid": BKID,
            "familyId": FB,
            "label": "zzb phone",
            "tokenHash": "zzbh",
            "fcmToken": "z",
        },
    )
    add(
        "bridgeConversations/bc1",
        {"ownerUid": KID, "familyId": FA, "uid": EXT, "conversationId": "c1"},
    )
    add(
        "bridgeConversations/bc2",
        {"ownerUid": BKID, "familyId": FB, "uid": BEXT, "conversationId": "c2"},
    )
    add(f"bridgePairCodes/{BR}", {"expiresAt": TS2})
    add(f"contactNames/{FA}_aaaa", {"uid": EXT, "familyId": FA})
    add(f"contactNames/{FB}_bbbb", {"uid": BEXT, "familyId": FB})
    add(
        "contactRequests/pgr-a1_r1",
        {
            "deviceId": DEV,
            "reqId": "r1",
            "ownerUid": KID,
            "name": "Zed",
            "phone": "+12065550123",
            "status": "pending",
        },
    )
    add(
        "contactRequests/pgr-zzb1_r1",
        {
            "deviceId": BDEV,
            "reqId": "r1",
            "ownerUid": BKID,
            "name": "Q",
            "phone": "+12065550124",
        },
    )

    dm = f"{KID}|{MOM}"
    add(
        f"conversations/{dm}",
        {
            "uids": [KID, MOM],
            "kind": "dm",
            "lastMessageAt": TS,
            "lastPreview": "hello 2",
            "unread": {},
            "familyIds": [FA],
            "participants": {KID: {"alias": "kida"}, MOM: {"alias": "moma"}},
        },
    )
    add(
        f"conversations/{GROUP}",
        {
            "uids": [KID, MOM, EXT],
            "kind": "group",
            "name": "Fam",
            "alias": "famgrp",
            "createdBy": KID,
            "familyIds": [FA],
            "unread": {},
            "lastMessageAt": TS,
            "lastPreview": "",
        },
    )
    add("aliases/famgrp", {"convKey": GROUP})
    cross = f"{BKID}|{KID}"
    add(
        f"conversations/{cross}",
        {
            "uids": [BKID, KID],
            "kind": "dm",
            "lastMessageAt": TS,
            "lastPreview": "x",
            "unread": {},
            "familyIds": sorted([FA, FB]),
        },
    )
    add(
        f"conversations/{BKID}|{BMOM}",
        {"uids": [BKID, BMOM], "kind": "dm", "familyIds": [FB], "unread": {}},
    )
    add("messages/m1", _msg(5, dm, [KID, MOM], KID, MOM, [FA]))
    add("messages/m2", _msg(6, dm, [KID, MOM], MOM, KID, [FA]))
    add(
        "messages/m3",
        _msg(6, GROUP, [KID, MOM], KID, MOM, [FA], groupMsgId="gm1", wireId="w6"),
    )
    add("messages/m4", _msg(7, cross, [BKID, KID], BKID, KID, sorted([FA, FB])))
    add("messages/m5", _msg(8, f"{BKID}|{BMOM}", [BKID, BMOM], BKID, BMOM, [FB]))
    add(f"wireIds/w5_{MOM}", {"messageId": "m1", "createdAt": TS})
    add(f"wireIds/w6_{KID}", {"messageId": "m2", "createdAt": TS})
    add(f"wireIds/w7_{KID}", {"messageId": "m4", "createdAt": TS})
    add("settings/meta", {"schemaVersion": 2, "seqCounter": 8, "lastSweepAt": None})
    add(
        "heldSms/h1",
        {
            "familyId": FA,
            "toUid": KID,
            "from": "+12065550777",
            "body": "held",
            "receivedAt": TS,
        },
    )
    add(
        "heldSms/h2",
        {
            "familyId": FB,
            "toUid": BKID,
            "from": "+12065550777",
            "body": "held b",
            "receivedAt": TS,
        },
    )
    add(
        "heldChat/hc1", {"familyId": FA, "toUid": KID, "body": "chat", "receivedAt": TS}
    )

    add("sessions/ss1", {"uid": KID, "lastSeenAt": TS})
    add("inviteCodes/ic1", {"uid": MOM, "createdBy": SUP, "expiresAt": TS2})
    add("rateLimits/ip_1.2.3.4", {"windowStart": TS})
    add("gchatSpaces/sp1", {"uid": KID})
    add("cells/c1", {"resolvedAt": TS})
    return r


def open_store(
    path: Path, records: list[dict[str, Any]] | None = None
) -> sqlitestore.Client:
    """A fresh SQLite store at `path` holding `records` (default: the two-family world)."""
    db = sqlitestore.open_client(str(path))
    dbexport.import_records(
        db, world_records() if records is None else records, merge=True
    )
    return db
