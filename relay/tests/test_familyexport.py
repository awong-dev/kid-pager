"""`app.familyexport` export side."""

from __future__ import annotations

import io
import json

import pytest

from app import familyexport
from app.familyexport import ExportOptions, KpfamFile
from tests import familymove_world as w


@pytest.fixture
def src(tmp_path):
    db = w.open_store(tmp_path / "src.sqlite")
    yield db
    db.close()


def _export(db, fid=w.FA, **kw) -> bytes:
    buf = io.BytesIO()
    familyexport.write_kpfam(
        db, fid, buf, passphrase=kw.pop("passphrase", None), opts=ExportOptions(**kw)
    )
    return buf.getvalue()


def _rows(data: bytes, passphrase: str | None = None) -> list[dict]:
    f = KpfamFile(io.BytesIO(data), passphrase)
    f.verify()
    return list(f.rows())


def _paths(rows) -> set[str]:
    return {r["path"] for r in rows}


def test_export_requires_frozen_unless_live(src):
    src.collection("families").document(w.FA).update({"frozen": False})
    with pytest.raises(familyexport.NotFrozen):
        _export(src)
    assert _rows(_export(src, backup=True))
    with pytest.raises(familyexport.FamilyNotFound):
        _export(src, fid="nope", backup=True)


def test_no_trace_of_the_other_family(src):
    data = _export(src)
    for passphrase in (None, "pw"):
        rows = _rows(
            _export(src, passphrase=passphrase) if passphrase else data, passphrase
        )
        for row in rows:
            text = json.dumps(row, ensure_ascii=False)
            for needle in w.B_STRINGS:
                assert needle not in text, (needle, row["path"])
    f = KpfamFile(io.BytesIO(data))
    m = f.manifest()
    assert "zzb" not in m.model_dump_json()
    assert m.family.id == w.FA and m.family.name == "Alpha" and m.family.idSeed == w.FA
    assert (
        m.kpfam == 1
        and m.schemaVersion == familyexport.SCHEMA_VERSION
        and m.history is True
    )


def test_contents_follow_the_registry(src):
    rows = _rows(_export(src))
    paths = _paths(rows)
    by_path = {r["path"]: r["data"] for r in rows}
    # data + secret + history travel
    for p in (
        f"families/{w.FA}",
        f"families/{w.FA}/alerts/a1",
        f"users/{w.KID}",
        f"users/{w.EXT}",
        f"users/{w.SUP}",
        f"users/{w.KID}/backends/bk1",
        f"users/{w.KID}/book/{w.MOM}",
        f"users/{w.MOM}/credentials/cred1",
        "aliases/kida",
        "aliases/xexta",
        "aliases/famgrp",
        f"smsNumbers/{w.NUM_KID}",
        f"allow/{w.KID}_{w.MOM}",
        f"allow/{w.KID}_{w.EXT}",
        f"devices/{w.DEV}",
        f"deviceSecrets/{w.DEV}",
        f"bridges/{w.BR}",
        "bridgeConversations/bc1",
        f"contactNames/{w.FA}_aaaa",
        "contactRequests/pgr-a1_r1",
        f"conversations/{w.KID}|{w.MOM}",
        f"conversations/{w.GROUP}",
        "messages/m1",
        "messages/m3",
        f"wireIds/w5_{w.MOM}",
        f"devices/{w.DEV}/locations/l1",
        f"devices/{w.DEV}/battery/b1",
        f"devices/{w.DEV}/smsLog/s1",
        "heldSms/h1",
        "heldChat/hc1",
    ):
        assert p in paths, p
    # skipped, cross-family, and other-family rows do not
    gone = (
        f"users/{w.MOM}/pushTokens/t1",
        f"users/{w.MOM}/pushSubscriptions/s1",
        "sessions/ss1",
        "inviteCodes/ic1",
        "rateLimits/ip_1.2.3.4",
        "gchatSpaces/sp1",
        "cells/c1",
        f"locReqs/{w.DEV}",
        f"locWireIds/{w.DEV}_x",
        f"bridges/{w.BR}/outbox/o1",
        "bridgePairCodes/" + w.BR,
        "settings/meta",
        "families/" + w.FB,
        f"allow/{w.KID}_{w.BKID}",
        f"allow/{w.BKID}_{w.KID}",
        f"conversations/{w.BKID}|{w.KID}",
        "messages/m4",
        f"wireIds/w7_{w.KID}",
        "families/fa/alerts/a2",
        f"users/{w.KID}/book/{w.BKID}",
        "devices/devices",
        f"devices/{w.DEV}/smsLog/s2",
    )
    for p in gone:
        assert p not in paths, p
    assert not any(p.startswith("setupCodes/") for p in paths)
    # fields: secrets kept, tokens and flags removed, foreign refs trimmed
    assert (
        "fcmToken" not in by_path[f"bridges/{w.BR}"]
        and by_path[f"bridges/{w.BR}"]["tokenHash"] == "th"
    )
    assert by_path[f"deviceSecrets/{w.DEV}"]["hmacKey"] == {"$b64": "a2V5"}
    assert "frozen" not in by_path[f"families/{w.FA}"]
    assert by_path[f"devices/{w.DEV}"]["locatableBy"] == [w.MOM]
    assert by_path["messages/m1"]["createdAt"] == w.TS


def test_dropped_is_counted_without_ids(src):
    m = KpfamFile(io.BytesIO(_export(src))).manifest()
    got = {(d.collection, d.count) for d in m.dropped}
    assert {
        ("allow", 2),
        ("conversations", 1),
        ("messages", 1),
        ("alerts", 1),
        ("book", 1),
        ("smsLog", 1),
    } <= got
    assert (
        m.counts["messages"] == 3
        and m.counts["users"] == 4
        and m.counts["families"] == 1
    )


def test_history_off_leaves_history_collections_out(src):
    rows = _rows(_export(src, history=False))
    colls = {r["path"].split("/")[-2] for r in rows}
    assert colls.isdisjoint(
        {"messages", "wireIds", "locations", "battery", "smsLog", "heldSms", "heldChat"}
    )
    assert {"conversations", "devices", "deviceSecrets", "users"} <= colls
    assert (
        KpfamFile(io.BytesIO(_export(src, history=False))).manifest().history is False
    )


def test_encrypted_round_trip_and_wrong_passphrase(src):
    plain = _export(src)
    enc = _export(src, passphrase="correct horse")
    assert enc.startswith(familyexport.MAGIC) and plain[:2] == b"\x1f\x8b"
    assert b"hello" not in enc
    assert _rows(enc, "correct horse") == _rows(plain)
    with pytest.raises(familyexport.BadPassphrase):
        KpfamFile(io.BytesIO(enc), "wrong").verify()
    with pytest.raises(familyexport.BadPassphrase):
        KpfamFile(io.BytesIO(enc), None).manifest()


def test_damage_is_detected(src):
    enc = _export(src, passphrase="pw")
    with pytest.raises(familyexport.CorruptFile):
        KpfamFile(io.BytesIO(enc[:-20]), "pw").verify()
    plain = _export(src)
    with pytest.raises(familyexport.CorruptFile):
        KpfamFile(io.BytesIO(plain[:-30])).verify()
    # a tampered row with a re-gzipped, unchanged trailer fails the checksum
    import gzip

    lines = gzip.decompress(plain).splitlines(keepends=True)
    assert b"u_" in lines[2]
    lines[2] = lines[2].replace(b"u_", b"v_", 1)
    with pytest.raises(familyexport.CorruptFile):
        KpfamFile(io.BytesIO(gzip.compress(b"".join(lines)))).verify()
    with pytest.raises(familyexport.CorruptFile):
        KpfamFile(io.BytesIO(b"not a file")).manifest()


def test_trailer_hash_covers_everything_before_it(src):
    import gzip
    import hashlib

    lines = gzip.decompress(_export(src)).splitlines(keepends=True)
    assert json.loads(lines[-1]) == {
        "sha256": hashlib.sha256(b"".join(lines[:-1])).hexdigest()
    }
    assert json.loads(lines[0])["kpfam"] == 1
