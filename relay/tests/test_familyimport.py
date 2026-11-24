"""`app.familyexport` import side.

Sources and targets are a fresh SQLite file ("sqlite") or the process-wide store ("global": SQLite,
or the Firestore emulator when the suite runs against it).
"""

from __future__ import annotations

import gzip
import io
import json
from datetime import UTC, datetime, timedelta

import pytest
from firebase_admin import auth as fb_auth

from app import dbexport, familyexport
from app.db.firestore import get_db
from app.familyexport import ImportOptions, ImportRefused, KpfamFile
from app.store import externals as externals_store
from app.store import families as families_store
from app.store import users as users_store
from tests import familymove_world as w

ENGINES = ["sqlite", "global"]


class RecordingAuth:
    """Stand-in for the auth backend seam."""

    def __init__(self, fail_on_claims: bool = False) -> None:
        self.created: list[tuple[str, str | None]] = []
        self.claims: dict[str, tuple[str, str | None]] = {}
        self.invites: list[tuple[str, str]] = []
        self.fail_on_claims = fail_on_claims
        self.owners: dict[str, str] = {}  # email -> uid already in the target's auth

    def create_account(self, uid, email):
        self.created.append((uid, email))

    def set_claims(self, uid, role, family_id):
        if self.fail_on_claims:
            raise RuntimeError("auth down")
        self.claims[uid] = (role, family_id)

    def email_owner(self, email):
        return self.owners.get(email)

    def mint_invite(self, uid, created_by):
        self.invites.append((uid, created_by))
        return f"CODE-{uid}", datetime.now(UTC) + timedelta(days=1)


def make_store(engine: str, tmp_path, name: str, records=None):
    if engine == "global":
        db = get_db()
        dbexport.import_records(db, records or [], merge=True)
        return db
    return w.open_store(
        tmp_path / f"{name}.sqlite", records if records is not None else []
    )


def dump_records(db):
    return (
        dbexport.export_sqlite(db._path)
        if hasattr(db, "_path")
        else dbexport.export_firestore(db)
    )


def dump(db) -> dict[str, dict]:
    return {r["path"]: r["data"] for r in dump_records(db)}


def bundle(db, passphrase=None, fid=w.FA, **kw) -> KpfamFile:
    buf = io.BytesIO()
    familyexport.write_kpfam(
        db,
        fid,
        buf,
        passphrase=passphrase,
        opts=familyexport.ExportOptions(backup=True, **kw),
    )
    return KpfamFile(io.BytesIO(buf.getvalue()), passphrase)


@pytest.fixture(params=ENGINES)
def source_engine(request):
    return request.param


@pytest.fixture
def src_db(tmp_path):
    db = w.open_store(tmp_path / "src.sqlite")
    yield db
    db.close()


HOME = ImportOptions(target_kind="home", super_alias="moma")
CLOUD = ImportOptions(
    target_kind="cloud", emails={"moma": "mom@example.com", "kida": "-"}
)


def test_round_trip_sqlite_to_sqlite_home(tmp_path, src_db):
    dst = make_store("sqlite", tmp_path, "dst")
    res = familyexport.apply_import(dst, bundle(src_db), HOME, RecordingAuth())
    assert res.report.ok and res.promoted == "moma"
    got = dump(dst)
    want = {r["path"]: r["data"] for r in w.world_records()}
    # users, identity rows and conversations arrive unchanged (except role / marker fixups)
    for p in (
        f"users/{w.KID}",
        f"users/{w.EXT}",
        "aliases/kida",
        "aliases/famgrp",
        f"allow/{w.KID}_{w.MOM}",
        f"conversations/{w.GROUP}",
        "messages/m1",
        f"wireIds/w5_{w.MOM}",
        f"deviceSecrets/{w.DEV}",
        f"users/{w.MOM}/credentials/cred1",
        f"devices/{w.DEV}/locations/l1",
        "heldSms/h1",
        f"users/{w.KID}/book/{w.MOM}",
        f"smsNumbers/{w.NUM_KID}",
        f"contactNames/{w.FA}_aaaa",
    ):
        assert p in got, p
    assert got[f"users/{w.KID}"] == want[f"users/{w.KID}"]
    assert got[f"deviceSecrets/{w.DEV}"] == want[f"deviceSecrets/{w.DEV}"]
    assert (
        got[f"users/{w.MOM}/credentials/cred1"]
        == want[f"users/{w.MOM}/credentials/cred1"]
    )
    assert (
        got[f"families/{w.FA}"]["importing"] is False
        and "frozen" not in got[f"families/{w.FA}"]
    )
    # nothing of the other family, nothing skipped
    assert not any("zzb" in json.dumps(d) or "zzb" in p for p, d in got.items())
    assert not any(
        p.startswith(("sessions/", "inviteCodes/")) for p in got if p != "inviteCodes"
    )
    assert f"users/{w.MOM}/pushTokens/t1" not in got


def test_device_and_bridge_fixups(tmp_path, src_db):
    dst = make_store("sqlite", tmp_path, "dst")
    familyexport.apply_import(dst, bundle(src_db), HOME, RecordingAuth())
    got = dump(dst)
    dev = got[f"devices/{w.DEV}"]
    assert (
        dev["provisionState"] == "moved"
        and dev["status"] == {}
        and dev["bookVersion"] == 4
    )
    assert not [k for k in dev if k.startswith("pending")]
    assert dev["locatableBy"] == [w.MOM]
    br = got[f"bridges/{w.BR}"]
    assert "fcmToken" not in br and br["lastSeenAt"] is None and br["health"] is None
    # unpaired, so the existing "new pair code" route works on it
    assert br["tokenHash"] is None


@pytest.mark.parametrize("src_kind", ENGINES)
@pytest.mark.parametrize("dst_kind", ENGINES)
def test_cross_engine_matrix(tmp_path, src_kind, dst_kind):
    if src_kind == dst_kind == "global":
        pytest.skip("one process-wide store")
    src = make_store(src_kind, tmp_path, "s", w.world_records())
    bun = bundle(src)
    if src_kind == "global":
        # empty the global store so the target engine is exercised on its own
        for r in dump_records(src):
            parent, _, did = r["path"].rpartition("/")
            src.collection(parent).document(did).delete()
    dst = make_store(dst_kind, tmp_path, "d")
    res = familyexport.apply_import(dst, bun, HOME, RecordingAuth())
    assert res.report.counts["users"] == 4
    assert dump(dst)[f"users/{w.KID}"]["smsNumber"] == w.NUM_KID


def test_dry_run_writes_nothing_and_reports(tmp_path, src_db):
    dst = make_store(
        "sqlite", tmp_path, "dst", [{"path": "families/zzz", "data": {"name": "z"}}]
    )
    before = dump(dst)
    rep = familyexport.plan_import(dst, bundle(src_db), HOME)
    assert rep.ok and rep.targetFamilyId == w.FA and rep.counts["messages"] == 3
    assert any(d.collection == "allow" for d in rep.dropped)
    assert dump(dst) == before


def test_family_id_taken_needs_as_and_idseed_keeps_sms_contacts(tmp_path):
    """Contact ids derive from `idSeed or id`, so a remapped import still finds the contact."""
    number = "+12065550222"
    uid, alias = externals_store.contact_ids("fa", number)
    recs = [
        {"path": "families/fa", "data": {"name": "Alpha", "frozen": True}},
        {"path": f"users/{w.KID}", "data": w._user(w.KID, "kida", familyId="fa")},
        {"path": "aliases/kida", "data": {"uid": w.KID}},
        {
            "path": f"users/{uid}",
            "data": {
                "alias": alias,
                "displayName": "Gran",
                "kind": "external",
                "ownerFamilyId": "fa",
                "familyId": None,
                "phone": number,
                "role": "member",
                "smsNumber": None,
            },
        },
        {"path": f"aliases/{alias}", "data": {"uid": uid}},
        {"path": "contactNames/fa_0123", "data": {"uid": uid, "familyId": "fa"}},
        {
            "path": "families/fa/alerts/a1",
            "data": {
                "kind": "x",
                "status": "open",
                "subjectUid": w.KID,
                "peerUid": uid,
            },
        },
    ]
    src = w.open_store(tmp_path / "s.sqlite", recs)
    bun = bundle(src, fid="fa")

    families_store.create_family(
        name="Home", created_by="r", family_id="fa"
    )  # the target's `fa`
    dst = get_db()
    plain = familyexport.plan_import(
        dst, bun, ImportOptions(target_kind="home", super_alias="kida")
    )
    assert not plain.ok and [(c.kind, c.id) for c in plain.collisions] == [
        ("family", "fa")
    ]

    opts = ImportOptions(target_kind="home", as_id="fa2", super_alias="kida")
    with pytest.raises(ImportRefused):  # --super must be an admin of the file
        familyexport.apply_import(dst, bun, opts, RecordingAuth())
    opts = ImportOptions(target_kind="home", as_id="fa2")
    # the target holds a super, so none is needed
    users_store.create_user(
        uid="rootsup", alias="rootsup", display_name="R", role="super", family_id="fa"
    )
    res = familyexport.apply_import(dst, bun, opts, RecordingAuth())
    assert res.report.targetFamilyId == "fa2"
    fam = families_store.get_family("fa2")
    assert fam and fam.idSeed == "fa" and fam.importing is False
    # the SMS contact is found by number under the new family id, and is the imported row
    found = externals_store.get_family_contact("fa2", number)
    assert found is not None and found.uid == uid and found.ownerFamilyId == "fa2"
    assert externals_store.get_or_create("fa2", number, "Gran").uid == uid
    # rewrites: family ids, the alerts path, the contactNames prefix
    got = dump(dst)
    assert "families/fa2/alerts/a1" in got and "contactNames/fa2_0123" in got
    assert got["contactNames/fa2_0123"]["familyId"] == "fa2"
    assert got[f"users/{w.KID}"]["familyId"] == "fa2"
    assert (
        families_store.get_family("fa").idSeed is None
    )  # the target's own family is untouched
    assert externals_store.contact_ids("fa2", number)[0] == uid


def test_alias_collision_with_and_without_rename(tmp_path):
    recs = w.world_records()
    for r in recs:
        if r["path"] == "messages/m3":
            r["data"]["senderAlias"] = "kida"
    src = w.open_store(tmp_path / "s.sqlite", recs)
    dst = make_store(
        "sqlite",
        tmp_path,
        "d",
        [
            {
                "path": "users/other",
                "data": {"alias": "kida", "role": "super", "familyId": "x"},
            },
            {"path": "aliases/kida", "data": {"uid": "other"}},
        ],
    )
    plain = familyexport.plan_import(
        dst, bundle(src), ImportOptions(target_kind="home")
    )
    assert [(c.kind, c.id) for c in plain.collisions] == [("alias", "kida")]
    with pytest.raises(ImportRefused):
        familyexport.apply_import(
            dst, bundle(src), ImportOptions(target_kind="home"), RecordingAuth()
        )

    ok = ImportOptions(target_kind="home", alias_map={"kida": "kida-a"})
    res = familyexport.apply_import(dst, bundle(src), ok, RecordingAuth())
    # the result lists what arrived, with the owner under the new alias
    assert [(d.id, d.label, d.ownerAlias) for d in res.devices] == [
        (w.DEV, "Kid pager", "kida-a")
    ]
    assert [(b.id, b.label, b.ownerAlias) for b in res.bridges] == [
        (w.BR, "phone", "kida-a")
    ]
    got = dump(dst)
    assert got["aliases/kida"] == {"uid": "other"}
    assert got["aliases/kida-a"] == {"uid": w.KID}
    assert got[f"users/{w.KID}"]["alias"] == "kida-a"
    assert got["messages/m3"]["senderAlias"] == "kida-a"
    assert (
        got[f"conversations/{w.KID}|{w.MOM}"]["participants"][w.KID]["alias"]
        == "kida-a"
    )
    assert (
        got[f"conversations/{w.KID}|{w.MOM}"]["participants"][w.MOM]["alias"] == "moma"
    )


def test_bad_alias_rename_is_refused(tmp_path, src_db):
    dst = make_store("sqlite", tmp_path, "d")
    rep = familyexport.plan_import(
        dst,
        bundle(src_db),
        ImportOptions(
            target_kind="home", alias_map={"nope": "x1", "kida": "Bad Alias"}
        ),
    )
    assert not rep.ok and len(rep.errors) >= 2


def test_other_collisions_abort(tmp_path, src_db):
    dst = make_store(
        "sqlite",
        tmp_path,
        "d",
        [
            {"path": f"users/{w.KID}", "data": {"alias": "other1", "role": "super"}},
            {"path": f"devices/{w.DEV}", "data": {"ownerUid": "z"}},
            {"path": f"bridges/{w.BR}", "data": {"ownerUid": "z"}},
            {"path": f"smsNumbers/{w.NUM_KID}", "data": {"uid": "z"}},
            {"path": f"allow/{w.KID}_{w.MOM}", "data": {}},
        ],
    )
    rep = familyexport.plan_import(
        dst, bundle(src_db), ImportOptions(target_kind="home")
    )
    assert {c.kind for c in rep.collisions} == {
        "uid",
        "device",
        "bridge",
        "number",
        "path",
    }
    assert not rep.ok


def test_seq_block_keeps_message_order_and_group_copies_and_creates_meta_on_an_empty_target(
    tmp_path, src_db
):
    dst = make_store(
        "sqlite",
        tmp_path,
        "d",
        [{"path": "settings/meta", "data": {"seqCounter": 100, "schemaVersion": 2}}],
    )
    familyexport.apply_import(dst, bundle(src_db), HOME, RecordingAuth())
    got = dump(dst)
    seq = {m: got[f"messages/{m}"]["seq"] for m in ("m1", "m2", "m3")}
    assert seq == {"m1": 101, "m2": 102, "m3": 102}  # old 5 < 6 == 6; sharing kept
    assert got["settings/meta"]["seqCounter"] == 102

    dst = make_store("sqlite", tmp_path, "d2")
    familyexport.apply_import(dst, bundle(src_db), HOME, RecordingAuth())
    got = dump(dst)
    assert got["settings/meta"]["seqCounter"] == 2 and got["messages/m1"]["seq"] == 1


def test_schema_version_gate(tmp_path, src_db, monkeypatch):
    monkeypatch.setattr(familyexport, "SCHEMA_VERSION", 3)
    bun = bundle(src_db)
    monkeypatch.setattr(familyexport, "SCHEMA_VERSION", 2)
    dst = make_store("sqlite", tmp_path, "d")
    rep = familyexport.plan_import(dst, bun, HOME)
    assert not rep.ok and "schema version" in rep.errors[0]


def test_encrypted_file_imports_and_history_off(tmp_path, src_db):
    dst = make_store("sqlite", tmp_path, "d")
    bun = bundle(src_db, passphrase="pw", history=False)
    res = familyexport.apply_import(dst, bun, HOME, RecordingAuth())
    assert "messages" not in res.report.counts and not res.report.history
    assert not any(p.startswith("messages/") for p in dump(dst))
    with pytest.raises(familyexport.BadPassphrase):
        familyexport.plan_import(
            dst, KpfamFile(io.BytesIO(bun._src.getvalue()), "no"), HOME
        )


def test_corrupt_file_writes_nothing(tmp_path, src_db):
    raw = gzip.decompress(bundle(src_db)._src.getvalue())
    bad = KpfamFile(io.BytesIO(gzip.compress(raw.replace(b"hello 5", b"hullo 5"))))
    dst = make_store("sqlite", tmp_path, "d")
    with pytest.raises(familyexport.CorruptFile):
        familyexport.apply_import(dst, bad, HOME, RecordingAuth())
    assert dump(dst) == {}


def test_home_target_roles_and_invites(tmp_path, src_db):
    dst = make_store("sqlite", tmp_path, "d")
    auth = RecordingAuth()
    res = familyexport.apply_import(
        dst, bundle(src_db), ImportOptions(target_kind="home", super_alias="moma"), auth
    )
    got = dump(dst)
    assert got[f"users/{w.MOM}"]["role"] == "super"
    assert got[f"users/{w.SUP}"]["role"] == "admin"  # imported super demoted
    # the credentials in the file make an invite unnecessary only for people who have them
    assert [i.alias for i in res.invites] == [
        "supa"
    ]  # moma has a passkey row in the file
    assert [u for u, _ in auth.invites] == [w.SUP]


def test_home_target_without_super_needs_super_alias(tmp_path, src_db):
    dst = make_store("sqlite", tmp_path, "d")
    rep = familyexport.plan_import(
        dst, bundle(src_db), ImportOptions(target_kind="home")
    )
    assert not rep.ok and "--super" in rep.errors[0]
    rep = familyexport.plan_import(
        dst, bundle(src_db), ImportOptions(target_kind="home", super_alias="kida")
    )
    assert not rep.ok  # a member cannot be promoted


def test_home_target_with_a_super_demotes_imported_super(tmp_path, src_db):
    dst = make_store(
        "sqlite",
        tmp_path,
        "d",
        [
            {
                "path": "users/boss",
                "data": {"alias": "boss", "role": "super", "familyId": "x"},
            }
        ],
    )
    familyexport.apply_import(
        dst, bundle(src_db), ImportOptions(target_kind="home"), RecordingAuth()
    )
    assert dump(dst)[f"users/{w.SUP}"]["role"] == "admin"


def test_cloud_target_creates_accounts_with_pinned_uid(tmp_path, src_db):
    dst = make_store("sqlite", tmp_path, "d")
    auth = RecordingAuth()
    opts = ImportOptions(
        target_kind="cloud",
        emails={"moma": "mom@example.com", "kida": "kid@example.com"},
    )
    res = familyexport.apply_import(dst, bundle(src_db), opts, auth)
    # supa's email comes from the file (prefill)
    assert sorted(auth.created) == sorted(
        [
            (w.MOM, "mom@example.com"),
            (w.KID, "kid@example.com"),
            (w.SUP, "sup@example.com"),
        ]
    )
    # the target has no super, so the imported super keeps `super`
    assert auth.claims[w.MOM] == ("admin", w.FA) and auth.claims[w.KID] == (
        "member",
        w.FA,
    )
    assert dump(dst)[f"users/{w.SUP}"]["role"] == "super"
    assert res.accountsPending == [] and res.invites == []
    got = dump(dst)
    assert got[f"users/{w.MOM}"]["email"] == "mom@example.com"
    assert f"users/{w.MOM}/credentials/cred1" not in got  # cloud has no passkeys


def test_cloud_target_with_super_demotes_and_needs_an_email(tmp_path, src_db):
    dst = make_store(
        "sqlite",
        tmp_path,
        "d",
        [
            {
                "path": "users/boss",
                "data": {"alias": "boss", "role": "super", "familyId": "x"},
            }
        ],
    )
    rep = familyexport.plan_import(
        dst, bundle(src_db), ImportOptions(target_kind="cloud")
    )
    assert not rep.ok and any("no email for" in e for e in rep.errors)
    familyexport.apply_import(dst, bundle(src_db), CLOUD, RecordingAuth())
    assert dump(dst)[f"users/{w.SUP}"]["role"] == "admin"


def _no_such_email(email):
    raise fb_auth.UserNotFoundError("none", None, None)


def test_firebase_seam_is_faked_not_called(tmp_path, src_db, monkeypatch):
    """The default cloud seam goes through `firebase_admin.auth`; the test replaces it."""
    calls: list = []
    monkeypatch.setattr(
        fb_auth, "create_user", lambda **kw: calls.append(("create", kw))
    )
    monkeypatch.setattr(fb_auth, "get_user_by_email", _no_such_email)
    monkeypatch.setattr(
        fb_auth,
        "set_custom_user_claims",
        lambda uid, c: calls.append(("claims", uid, c)),
    )
    dst = make_store("sqlite", tmp_path, "d")
    familyexport.apply_import(dst, bundle(src_db), CLOUD)
    assert ("create", {"uid": w.MOM, "email": "mom@example.com"}) in calls
    assert ("claims", w.MOM, {"role": "admin", "fam": w.FA}) in calls


def test_default_home_seam_writes_invite_codes_into_the_target(tmp_path, src_db):
    dst = make_store("sqlite", tmp_path, "d")
    res = familyexport.apply_import(dst, bundle(src_db), HOME)
    assert len(res.invites) == 1
    codes = [p for p in dump(dst) if p.startswith("inviteCodes/")]
    assert len(codes) == 1 and dump(dst)[codes[0]]["uid"] == w.SUP


def test_interrupted_import_leaves_the_marker_and_blocks_a_retry(tmp_path, src_db):
    dst = make_store("sqlite", tmp_path, "d")
    with pytest.raises(RuntimeError):
        familyexport.apply_import(
            dst, bundle(src_db), CLOUD, RecordingAuth(fail_on_claims=True)
        )
    assert dump(dst)[f"families/{w.FA}"]["importing"] is True
    rep = familyexport.plan_import(dst, bundle(src_db), CLOUD)
    assert not rep.ok and "partial import" in rep.errors[0]
