"""`app.deployexport`: the whole-deployment bundle for the over-the-air move (docs/MOVE_DESIGN.md §5).

Sources and targets are a fresh SQLite file ("sqlite") or the process-wide store ("global")."""

from __future__ import annotations

import base64
import copy
import hashlib
import io
import json
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from app import (
    authn_home,
    dbexport,
    deployexport,
    familyexport,
    homeinit,
    movekeys,
    push_webpush,
)
from app.broker_admin import MosquittoDynsecAdmin
from app.config import Settings
from app.db.firestore import get_db
from app.deployexport import (
    BrokerLoginFailed,
    DeploymentExportOptions,
    DeploymentImportOptions,
    DeploymentImportRefused,
    KpdepFile,
)
from app.emqx_admin import EmqxAdmin
from app.familyexport import BadPassphrase, CorruptFile, NotFrozen, PassphraseRequired
from app.store import devices as devices_store
from app.store import push_subscriptions as subs_store
from app.store import vapid as vapid_store
from tests import familymove_world as w

ENGINES = ["sqlite", "global"]
PASS = "correct horse battery staple"
TARGET_URL = "https://home.example/kid-pager"
MOVE_ID = "mv_0123456789ab"
FROZEN_AT = datetime(2026, 11, 26, 12, 0, tzinfo=UTC)

K_DEV = bytes(range(32))
K_BDEV = bytes(range(100, 132))
K_LEGACY = bytes(range(50, 82))
B64 = lambda b: base64.b64encode(b).decode("ascii")
OLD_PW_HASH = "ab" * 32
LEGACY = "pgr-old1"
NOW_US = int(time.time() * 1_000_000)
NOW = {"$ts": NOW_US}
HOUR_AGO = {"$ts": NOW_US - 3_600_000_000}
OLD = w.TS
PENDING = {"id": "m_1", "obj": {"v": 1, "kind": "cfg"}, "acked": False}
SRC_VAPID = vapid_store.VapidKey(privateKey="priv-src", publicKey="pub-src")


def world() -> list[dict[str, Any]]:
    """Two frozen families, with the device, bridge, message and account detail a move cares about."""
    recs = copy.deepcopy(w.world_records())
    by = {r["path"]: r["data"] for r in recs}

    def add(path: str, data: dict[str, Any]) -> None:
        recs.append({"path": path, "data": data})

    by[f"families/{w.FB}"]["frozen"] = True
    by[f"devices/{w.DEV}"].update(
        {
            "status": {"bearer": "direct", "mvc": 1, "bv": 3},
            "pendingBook": PENDING,
            "pendingCfgCa": PENDING,
            "pendingCfgMv": PENDING,
            "bookVersion": 3,
        }
    )
    by[f"deviceSecrets/{w.DEV}"].clear()
    by[f"deviceSecrets/{w.DEV}"].update({
        "hmacKey": B64(K_DEV),
        "mqttPasswordHash": OLD_PW_HASH,
        "upN": 7,
        "upBits": -1,
        "downN": 9,
        "sigFailures": 2,
        "createdAt": OLD,
    })
    by[f"deviceSecrets/{w.BDEV}"].clear()
    by[f"deviceSecrets/{w.BDEV}"].update({
        "hmacKey": B64(K_BDEV),
        "mqttPasswordHash": OLD_PW_HASH,
        "upN": 1,
        "downN": 1,
    })
    by[f"devices/{w.BDEV}"].update(
        {"authMode": "hmac", "provisionState": "provisioned", "mqttUsername": w.BDEV}
    )
    add(
        f"devices/{LEGACY}",
        {
            "ownerUid": w.KID,
            "label": "Old pager",
            "mqttUsername": LEGACY,
            "familyId": w.FA,
            "locatableBy": [],
            "status": {"bearer": "direct"},
            "authMode": "password",
            "provisionState": "provisioned",
            "pendingBook": PENDING,
            "bookVersion": 1,
        },
    )
    add(f"deviceSecrets/{LEGACY}", {"hmacKey": B64(K_LEGACY), "mqttPasswordHash": OLD_PW_HASH})

    by[f"bridges/{w.BR}"].update({"health": {"state": "ok"}})
    by[f"bridges/{w.BR}/outbox/o1"].update({"state": "sent"})
    add(f"bridges/{w.BR}/outbox/o2", {"text": "send me", "state": "pending", "to": {"phone": "+1"}})
    add(f"bridges/{w.BBR}/outbox/o9", {"text": "b", "state": "pending"})

    def delivery(state: str) -> dict[str, Any]:
        return {"pager:x": {"kind": "pager", "state": state, "attempts": 0}}

    dm = f"{w.KID}|{w.MOM}"
    for seq, mid, created, state, wid in (
        (9, "m_recent", NOW, "shown", "w9"),
        (10, "m_open", OLD, "queued", "w10"),
        (11, "m_old_done", OLD, "shown", "w11"),
        (12, "m_sent", OLD, "sent", "w12"),
    ):
        add(
            f"messages/{mid}",
            w._msg(seq, dm, [w.KID, w.MOM], w.KID, w.MOM, [w.FA], createdAt=created, deliveries=delivery(state), wireId=wid),
        )
        add(f"wireIds/{wid}_{w.MOM}", {"messageId": mid, "createdAt": created})
    add(f"wireIds/lone_{w.KID}", {"messageId": "gone", "createdAt": HOUR_AGO})
    add(f"wireIds/stale_{w.KID}", {"messageId": "gone", "createdAt": OLD})
    add("settings/retention", {"messagesDays": 30})
    add("settings/federation", {"enabled": True})
    add("settings/move", {"id": "mv_should_not_travel"})
    add("hooks/hk1", {"name": "h", "url": "https://hook.example/x", "secret": "s", "enabled": True})
    add(f"users/{w.KID}/credentials/password", {"kind": "password", "hash": "$old", "updatedAt": OLD})
    add(f"users/{w.MOM}/pushSubscriptions/s2", {"endpoint": "https://push.example/b", "p256dh": "p", "auth": "a"})

    priv, pub = "fed-private", "fed-public"
    add(f"fedFamilies/{w.FA}", {"familyId": w.FA, "fedId": "f_fa", "privateKey": priv, "publicKey": pub, "epoch": 1})
    add(
        f"fedLinks/{w.FA}_f_remote",
        {
            "familyId": w.FA,
            "remote": {"fedId": "f_remote", "name": "R", "publicKey": "k", "host": "r.example"},
            "shared": [w.KID],
        },
    )
    add("fedOutbox/ob-pending", {"familyId": w.FA, "linkId": f"{w.FA}_f_remote", "state": "pending"})
    add("fedOutbox/ob-acked", {"familyId": w.FA, "linkId": f"{w.FA}_f_remote", "state": "acked"})
    return recs


def make_store(engine: str, tmp_path, name: str, records=None):
    if engine == "global":
        db = get_db()
        dbexport.import_records(db, records or [], merge=True)
        return db
    return w.open_store(tmp_path / f"{name}.sqlite", records if records is not None else [])


def dump(db) -> dict[str, dict]:
    recs = dbexport.export_sqlite(db._path) if hasattr(db, "_path") else dbexport.export_firestore(db)
    return {r["path"]: r["data"] for r in recs}


class RecordingAdmin:
    """`BrokerAdmin` stand-in."""

    def __init__(self, outcome: str = "ok", fail_on: str | None = None) -> None:
        self.calls: list[tuple[str, str, str]] = []
        self.outcome = outcome
        self.fail_on = fail_on

    def ensure_device(self, username, password, device_id):
        self.calls.append((username, password, device_id))
        return "error" if device_id == self.fail_on else self.outcome

    def ensure_boot_user(self, bid, password):
        return "ok"

    def delete_user(self, username):
        return "ok"


class RecordingAuth:
    def __init__(self) -> None:
        self.created: list[tuple[str, str | None]] = []
        self.claims: dict[str, tuple[str, str | None]] = {}
        self.owners: dict[str, str] = {}

    def create_account(self, uid, email):
        self.created.append((uid, email))

    def set_claims(self, uid, role, family_id):
        self.claims[uid] = (role, family_id)

    def email_owner(self, email):
        return self.owners.get(email)

    def mint_invite(self, uid, created_by):
        raise AssertionError("a deployment import mints nothing")


def export_opts(**kw: Any) -> DeploymentExportOptions:
    base: dict[str, Any] = {
        "move_id": MOVE_ID,
        "frozen_at": FROZEN_AT,
        "target_base_url": TARGET_URL,
        "target_auth_backend": "home",
        "source_kind": "cloud",
        "base_url": "https://cloud.example",
        "vapid": SRC_VAPID,
        "env_secrets": ["SORACOM_AUTH_KEY"],
    }
    base.update(kw)
    return DeploymentExportOptions(**base)


def bundle_bytes(db, passphrase: str = PASS, **kw: Any) -> bytes:
    buf = io.BytesIO()
    deployexport.write_kpdep(db, buf, passphrase=passphrase, opts=export_opts(**kw))
    return buf.getvalue()


def open_bundle(data: bytes, passphrase: str | None = PASS) -> KpdepFile:
    return KpdepFile(io.BytesIO(data), passphrase)


def plaintext(data: bytes, passphrase: str = PASS) -> str:
    return b"".join(open_bundle(data, passphrase).lines()).decode()


HOME = DeploymentImportOptions(target_kind="home", public_base_url=TARGET_URL)


def do_import(dst, data: bytes, admin=None, opts=HOME, auth=None):
    return deployexport.apply_deployment_import(
        dst, open_bundle(data), opts, auth or RecordingAuth(), broker_admin=admin or RecordingAdmin()
    )


@pytest.fixture
def src_db(tmp_path):
    db = make_store("sqlite", tmp_path, "src", world())
    yield db
    db.close()


def people_invites(db) -> dict[str, str]:
    """A claim digest for the persons the world's tests name."""
    return {uid: authn_home.new_invite_code()[1] for uid in (w.MOM, w.KID)}


@pytest.mark.parametrize("src_kind", ENGINES)
@pytest.mark.parametrize("dst_kind", ENGINES)
def test_round_trip_home(tmp_path, src_kind, dst_kind):
    if src_kind == dst_kind == "global":
        pytest.skip("one process-wide store")
    src = make_store(src_kind, tmp_path, "s", world())
    invites = {w.MOM: authn_home.new_invite_code()[1], w.KID: authn_home.new_invite_code()[1]}
    hashes_ = {w.KID: authn_home.hash_password("a password")}
    data = bundle_bytes(src, invites=invites, password_hashes=hashes_)
    if src_kind == "global":
        for p in list(dump(src)):
            parent, _, did = p.rpartition("/")
            src.collection(parent).document(did).delete()
    dst = make_store(dst_kind, tmp_path, "d")
    admin = RecordingAdmin()
    res = do_import(dst, data, admin)
    assert res.report.ok and res.report.moveId == MOVE_ID
    got = dump(dst)
    want = {r["path"]: r["data"] for r in world()}

    # both families, live
    for fid in (w.FA, w.FB):
        assert got[f"families/{fid}"]["importing"] is False
        assert "frozen" not in got[f"families/{fid}"]
    assert got["settings/move"]["id"] == MOVE_ID and got["settings/move"]["role"] == "target"
    assert got["settings/move"]["sourceBaseUrl"] == "https://cloud.example"
    assert got["settings/move"]["bridgeRedirect"] is None

    # devices: moving, derived key stored as the key, login created with the derived password
    for dev, k in ((w.DEV, K_DEV), (w.BDEV, K_BDEV)):
        k2, pw2 = movekeys.derive(k, MOVE_ID, dev)
        d = got[f"devices/{dev}"]
        assert d["provisionState"] == "moving"
        assert d["move"] == {"id": MOVE_ID, "state": "waiting"}
        s = got[f"deviceSecrets/{dev}"]
        assert base64.b64decode(s["hmacKey"]) == k2
        assert "mqttPassword" not in s
        assert s["mqttPasswordHash"] == hashlib.sha256(pw2.encode()).hexdigest()
        assert s["downN"] == 0
        assert (dev, pw2, dev) in admin.calls
    s = got[f"deviceSecrets/{w.DEV}"]
    assert (s["upN"], s["upBits"], s["sigFailures"]) == (7, -1, 0)
    d = got[f"devices/{w.DEV}"]
    assert d["status"] == want[f"devices/{w.DEV}"]["status"]
    assert d["pendingBook"] == PENDING
    assert "pendingCfgCa" not in d and "pendingCfgMv" not in d
    assert d["bookVersion"] == 4
    assert d["locatableBy"] == [w.MOM, w.BKID]
    assert sorted(c[0] for c in admin.calls) == [w.DEV, w.BDEV]
    assert res.loginsCreated == [w.DEV, w.BDEV]

    # a pager with no HMAC key arrives as after a family move
    leg = got[f"devices/{LEGACY}"]
    assert leg["provisionState"] == "moved" and leg["status"] == {}
    assert not [k for k in leg if k.startswith("pending")]
    assert f"deviceSecrets/{LEGACY}" not in got and "move" not in leg
    assert res.devicesNeedingSetup == [LEGACY]
    assert res.report.devices.model_dump() == {"total": 3, "moving": 2, "setupCode": 1}

    # bridges: token kept, pending outbox rows carried under their ids
    br = got[f"bridges/{w.BR}"]
    assert br["tokenHash"] == "th"
    assert not {"fcmToken", "health", "lastSeenAt"} & br.keys()
    assert got[f"bridges/{w.BR}/outbox/o2"]["state"] == "pending"
    assert f"bridges/{w.BR}/outbox/o1" not in got
    assert f"bridges/{w.BBR}/outbox/o9" in got

    # people and accounts
    assert got[f"users/{w.MOM}/credentials/cred1"] == want[f"users/{w.MOM}/credentials/cred1"]
    pw = got[f"users/{w.KID}/credentials/password"]
    assert pw["hash"] == hashes_[w.KID] and pw["mustChange"] is True and pw["kind"] == "password"
    for uid, digest in invites.items():
        row = got[f"inviteCodes/{digest}"]
        assert row["uid"] == uid and row["createdBy"] == "move"
    assert not any(p.startswith("sessions/") for p in got)
    assert got[f"users/{w.MOM}/pushSubscriptions/s1"]["vapidKid"] == "src"
    assert got[f"users/{w.MOM}/pushSubscriptions/s2"]["vapidKid"] == "src"
    assert got["vapid/src"] == SRC_VAPID.model_dump()
    assert not any(p.startswith(f"users/{w.MOM}/pushTokens") for p in got)

    # deployment section
    assert got["settings/retention"] == {"messagesDays": 30}
    assert got["settings/federation"] == {"enabled": True}
    assert "settings/move" in got and got["settings/move"]["id"] == MOVE_ID
    assert got["settings/meta"]["seqCounter"] >= 4  # only the target's own counters
    assert got["hooks/hk1"]["enabled"] is False and got["hooks/hk1"]["secret"] == "s"

    # federation: key rotated, link announces, pending outbox row carried
    fed = got[f"fedFamilies/{w.FA}"]
    assert fed["epoch"] == 2 and fed["privateKey"] != "fed-private" and fed["prevPrivateKey"] == "fed-private"
    assert got[f"fedLinks/{w.FA}_f_remote"]["announce"]["pending"] is True
    assert "fedOutbox/ob-pending" in got and "fedOutbox/ob-acked" not in got

    # nothing from before the move leaks in
    assert f"users/{w.MOM}/credentials/password" not in got
    assert not any(c.collection in ("conversations", "messages") for c in res.report.dropped)


def test_imported_moving_device_reads_through_the_store(tmp_path, src_db):
    dst = make_store("global", tmp_path, "d")
    do_import(dst, bundle_bytes(src_db))
    dev = devices_store.get_device(w.DEV)
    assert dev is not None and dev.provisionState == "moving"
    assert dev.move is not None and (dev.move.id, dev.move.state) == (MOVE_ID, "waiting")
    assert devices_store.get_device(LEGACY).provisionState == "moved"  # type: ignore[union-attr]
    assert {d.id for d in devices_store.list_devices()} == {w.DEV, w.BDEV, LEGACY}


def test_no_original_device_key_in_the_bundle(src_db):
    data = bundle_bytes(src_db)
    text = plaintext(data)
    for k in (K_DEV, K_BDEV, K_LEGACY):
        assert B64(k) not in text and k.hex() not in text
        assert base64.urlsafe_b64encode(k).decode() not in text
    k2, pw2 = movekeys.derive(K_DEV, MOVE_ID, w.DEV)
    assert B64(k2) in text and pw2 in text
    # the source's own password hash is not carried either
    assert OLD_PW_HASH not in text
    # the legacy device has no secret row at all
    assert f"deviceSecrets/{LEGACY}" not in text
    # the container is encrypted
    assert data.startswith(familyexport.MAGIC) and pw2.encode() not in data


def test_manifest_shape(src_db):
    m = open_bundle(bundle_bytes(src_db, no_sign_in=[w.KID])).manifest()
    assert m.kpdep == 1 and m.schemaVersion == familyexport.SCHEMA_VERSION
    assert m.move.id == MOVE_ID and m.move.target.baseUrl == TARGET_URL
    assert m.move.target.authBackend == "home" and m.move.frozenAt.startswith("2026-11-26")
    assert [f.id for f in m.families] == [w.FA, w.FB]
    assert m.noSignIn == [w.KID] and m.envSecrets == ["SORACOM_AUTH_KEY"]
    assert m.source.kind == "cloud"


def test_passphrase_is_mandatory(src_db):
    with pytest.raises(PassphraseRequired):
        deployexport.export_deployment(src_db, export_opts())
    with pytest.raises(PassphraseRequired):
        deployexport.write_kpdep(src_db, io.BytesIO(), passphrase="", opts=export_opts())


def test_wrong_and_missing_passphrase(src_db):
    data = bundle_bytes(src_db)
    with pytest.raises(BadPassphrase):
        open_bundle(data, "nope").verify()
    with pytest.raises(BadPassphrase):
        open_bundle(data, None).verify()
    with pytest.raises(BadPassphrase):
        deployexport.plan_deployment_import(get_db(), open_bundle(data, "nope"), HOME)


def test_tampered_bundle(tmp_path, src_db):
    data = bytearray(bundle_bytes(src_db))
    data[len(data) // 2] ^= 0x01
    with pytest.raises((BadPassphrase, CorruptFile)):
        open_bundle(bytes(data)).verify()
    with pytest.raises(CorruptFile):
        open_bundle(bytes(data[:-40])).verify()
    # a re-encrypted file whose rows were edited fails its checksum
    lines = list(open_bundle(bundle_bytes(src_db)).lines())
    i = next(n for n, line in enumerate(lines) if b'"label":"Kid pager"' in line)
    lines[i] = lines[i].replace(b"Kid pager", b"Evil pager")
    forged = b"".join(familyexport.pack(lines, PASS))
    dst = make_store("sqlite", tmp_path, "d")
    with pytest.raises(CorruptFile, match="checksum"):
        deployexport.plan_deployment_import(dst, open_bundle(forged), HOME)
    assert dump(dst) == {}


def test_every_family_must_be_frozen(src_db):
    src_db.collection("families").document(w.FB).update({"frozen": False})
    with pytest.raises(NotFrozen):
        deployexport.export_deployment(src_db, export_opts(), encrypted=True)


def test_floor_without_history(src_db):
    full = plaintext(bundle_bytes(src_db))
    floor_rows = plaintext(bundle_bytes(src_db, history=False))
    # recent, and undelivered however old; the old finished one and everything else is history
    for mid in ("m_recent", "m_open", "m_sent"):
        assert f'"path":"messages/{mid}"' in floor_rows
    assert '"path":"messages/m_old_done"' not in floor_rows
    assert '"path":"messages/m_old_done"' in full
    # every wire id of the last 24 h, linked to a message or not
    assert '"path":"wireIds/lone_' + w.KID in floor_rows
    assert '"path":"wireIds/w9_' + w.MOM in floor_rows
    assert '"path":"wireIds/stale_' + w.KID not in floor_rows
    # the other history is left out, the rest of the family is not
    assert "/locations/" in full and "/locations/" not in floor_rows
    assert '"path":"heldSms/h1"' in full and '"path":"heldSms/h1"' not in floor_rows
    assert '"path":"users/' + w.KID + '"' in floor_rows
    assert '"path":"deviceSecrets/' + w.DEV + '"' in floor_rows


def test_floor_survives_an_import(tmp_path, src_db):
    dst = make_store("sqlite", tmp_path, "d")
    res = do_import(dst, bundle_bytes(src_db, history=False))
    got = dump(dst)
    assert {"messages/m_recent", "messages/m_open", "messages/m_sent"} <= got.keys()
    assert "messages/m_old_done" not in got
    assert f"wireIds/lone_{w.KID}" in got
    assert any("only the last 24 hours" in n for n in res.report.notes)
    # message ids and times are kept, sequence numbers renumbered
    assert got["messages/m_open"]["deliveries"]["pager:x"]["state"] == "queued"
    assert got["messages/m_open"]["createdAt"] == OLD


def test_dry_run_writes_nothing_and_calls_no_broker(tmp_path, src_db):
    dst = make_store("sqlite", tmp_path, "d")
    report = deployexport.plan_deployment_import(dst, open_bundle(bundle_bytes(src_db)), HOME, RecordingAuth())
    assert report.ok and report.devices.moving == 2
    assert report.counts["devices"] == 3 and report.counts["messages"] >= 4
    assert any("SORACOM_AUTH_KEY" in n for n in report.notes) and report.envSecrets == ["SORACOM_AUTH_KEY"]
    assert dump(dst) == {}


def test_bundle_for_another_server_is_refused(tmp_path, src_db):
    data = bundle_bytes(src_db)
    dst = make_store("sqlite", tmp_path, "d")
    other = DeploymentImportOptions(target_kind="home", public_base_url="https://elsewhere.example")
    with pytest.raises(DeploymentImportRefused) as ei:
        do_import(dst, data, opts=other)
    assert any("addressed to" in e for e in ei.value.report.errors)
    assert dump(dst) == {}
    # trailing slash and case do not matter
    ok = DeploymentImportOptions(target_kind="home", public_base_url="HTTPS://Home.Example/kid-pager/")
    assert deployexport.plan_deployment_import(dst, open_bundle(data), ok).ok


def test_missing_public_base_url_and_schema_mismatch(tmp_path, monkeypatch, src_db):
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    dst = make_store("sqlite", tmp_path, "d")
    rep = deployexport.plan_deployment_import(
        dst, open_bundle(bundle_bytes(src_db)), DeploymentImportOptions(target_kind="home")
    )
    assert any("PUBLIC_BASE_URL" in e for e in rep.errors)
    data = bundle_bytes(src_db)
    monkeypatch.setattr(deployexport, "SCHEMA_VERSION", 99)
    rep = deployexport.plan_deployment_import(dst, open_bundle(data), HOME)
    assert any("schema version mismatch" in e for e in rep.errors)


def test_reimport_and_non_empty_targets_are_refused(tmp_path, src_db):
    data = bundle_bytes(src_db)
    dst = make_store("sqlite", tmp_path, "d")
    do_import(dst, data)
    before = dump(dst)
    admin = RecordingAdmin()
    with pytest.raises(DeploymentImportRefused) as ei:
        do_import(dst, data, admin)
    rep = ei.value.report
    assert any("already holds 2 family" in e for e in rep.errors)
    kinds = {c.kind for c in rep.collisions}
    assert {"uid", "device", "bridge", "alias", "fed"} <= kinds
    assert admin.calls == [] and dump(dst) == before

    # any other server that already holds a family
    other = make_store("sqlite", tmp_path, "o", [{"path": "families/default", "data": {"name": "d"}}])
    with pytest.raises(DeploymentImportRefused) as ei:
        do_import(other, data)
    assert any("empty server" in e for e in ei.value.report.errors)
    assert "families/fa" not in dump(other)

    # a colliding device id on an otherwise empty server
    clash = make_store(
        "sqlite", tmp_path, "c", [{"path": f"devices/{w.DEV}", "data": {"label": "x"}}]
    )
    with pytest.raises(DeploymentImportRefused) as ei:
        do_import(clash, data)
    assert [(c.kind, c.id) for c in ei.value.report.collisions] == [("device", w.DEV)]


def test_broker_login_failure_writes_nothing(tmp_path, src_db):
    dst = make_store("sqlite", tmp_path, "d")
    with pytest.raises(BrokerLoginFailed) as ei:
        do_import(dst, bundle_bytes(src_db), RecordingAdmin(fail_on=w.BDEV))
    assert ei.value.device_id == w.BDEV
    assert dump(dst) == {}


def test_logins_left_to_hand_are_reported(tmp_path, src_db):
    dst = make_store("sqlite", tmp_path, "d")
    res = do_import(dst, bundle_bytes(src_db), RecordingAdmin(outcome="manual"))
    assert res.loginsManual == [w.DEV, w.BDEV] and res.loginsCreated == []


def test_logins_over_emqx_rest(tmp_path, src_db, monkeypatch):
    calls: list[tuple[str, str, Any]] = []

    def fake_request(method, url, *, json=None, auth=None, timeout=None):
        calls.append((method, url, json))
        return httpx.Response(200, text="{}")

    monkeypatch.setattr(httpx, "request", fake_request)
    admin = EmqxAdmin(
        Settings(
            broker_api_url="http://emqx.test/api/v5",
            broker_api_key="k",
            broker_api_secret="s",
            webhook_key="w",
            dev_mode=False,
            google_cloud_project=None,
            firestore_emulator_host=None,
            firebase_auth_emulator_host=None,
        ),
        manages_auth=True,
    )
    dst = make_store("sqlite", tmp_path, "d")
    do_import(dst, bundle_bytes(src_db), admin)
    _, pw = movekeys.derive(K_DEV, MOVE_ID, w.DEV)
    users = [c for c in calls if c[0] == "POST" and c[1].endswith("/users")]
    assert {"user_id": w.DEV, "password": pw, "is_superuser": False} in [u[2] for u in users]
    acls = [c for c in calls if c[0] == "PUT" and c[1].endswith(f"/users/{w.DEV}")]
    assert acls and {r["topic"] for r in acls[-1][2]["rules"]} >= {f"pager/{w.DEV}/up", f"pager/{w.DEV}/down"}


class FakeDynsec:
    def __init__(self) -> None:
        self.commands: list[dict[str, Any]] = []

    def control(self, commands, timeout=None):
        self.commands.extend(commands)
        return [{} for _ in commands]


def test_logins_over_mosquitto_dynsec(tmp_path, src_db):
    fake = FakeDynsec()
    admin = MosquittoDynsecAdmin(fake, manages_auth=True)  # type: ignore[arg-type]
    dst = make_store("sqlite", tmp_path, "d")
    do_import(dst, bundle_bytes(src_db), admin)
    _, pw = movekeys.derive(K_BDEV, MOVE_ID, w.BDEV)
    client = next(c for c in fake.commands if c["command"] == "createClient" and c["username"] == w.BDEV)
    assert client["password"] == pw and client["roles"] == [{"rolename": w.BDEV}]
    role = next(c for c in fake.commands if c["command"] == "createRole" and c["rolename"] == w.BDEV)
    assert any(a["topic"] == f"pager/{w.BDEV}/up" for a in role["acls"])


def test_firebase_target_needs_an_answer_per_person(tmp_path, src_db):
    fb = DeploymentImportOptions(target_kind="cloud", public_base_url=TARGET_URL)
    data = bundle_bytes(src_db, target_auth_backend="firebase")
    dst = make_store("sqlite", tmp_path, "d")
    rep = deployexport.plan_deployment_import(dst, open_bundle(data), fb, RecordingAuth())
    assert not rep.ok and rep.emailsMissing == ["kida", "kidzzb", "momzzb"]
    # the source's own emails default; the panel's "no sign-in" list answers the rest
    data = bundle_bytes(
        src_db, target_auth_backend="firebase", no_sign_in=[w.KID, w.BKID, w.BMOM]
    )
    auth = RecordingAuth()
    res = do_import(dst, data, opts=fb, auth=auth)
    assert res.report.ok
    assert sorted(a for a, _ in auth.created) == sorted([w.MOM, w.SUP])
    assert dict(auth.created)[w.MOM] == "mom@example.com"
    assert auth.claims[w.SUP] == ("super", w.FA) and auth.claims[w.MOM] == ("admin", w.FA)
    assert res.accountsCreated == ["moma", "supa"]
    assert res.accountsPending == ["kida", "kidzzb", "momzzb"]
    got = dump(dst)
    assert got[f"users/{w.SUP}"]["role"] == "super"
    assert not any("/credentials/" in p for p in got)  # home credentials are dropped
    assert not any(p.startswith("inviteCodes/") for p in got)
    assert got["settings/move"]["noSignIn"] == sorted([w.KID, w.BKID, w.BMOM])


def test_firebase_target_email_overrides_and_checks(tmp_path, src_db):
    fb = lambda **kw: DeploymentImportOptions(target_kind="cloud", public_base_url=TARGET_URL, **kw)
    data = bundle_bytes(src_db, target_auth_backend="firebase")
    dst = make_store("sqlite", tmp_path, "d")
    auth = RecordingAuth()
    rep = deployexport.plan_deployment_import(
        dst,
        open_bundle(data),
        fb(emails={"kida": "kid@example.com", "kidzzb": "-", "momzzb": "-"}),
        auth,
    )
    assert rep.ok
    assert {p.alias: p.email for p in rep.people}["kida"] == "kid@example.com"
    auth.owners["kid@example.com"] = "someone-else"
    rep = deployexport.plan_deployment_import(
        dst, open_bundle(data), fb(emails={"kida": "kid@example.com"}), auth
    )
    assert rep.emailsTaken == ["kida"] and not rep.ok
    rep = deployexport.plan_deployment_import(dst, open_bundle(data), fb(emails={"kida": "nope"}), auth)
    assert rep.emailsInvalid == ["kida"]
    rep = deployexport.plan_deployment_import(dst, open_bundle(data), fb(emails={"zed": "z@example.com"}), auth)
    assert any("no such person" in e for e in rep.errors)
    # the email answers mean nothing for a home target
    rep = deployexport.plan_deployment_import(
        dst, open_bundle(bundle_bytes(src_db)), DeploymentImportOptions(target_kind="home", public_base_url=TARGET_URL, emails={"kida": "-"})
    )
    assert any("Firebase target only" in e for e in rep.errors)


def test_target_backend_must_match_the_bundle(tmp_path, src_db):
    dst = make_store("sqlite", tmp_path, "d")
    rep = deployexport.plan_deployment_import(
        dst, open_bundle(bundle_bytes(src_db)), DeploymentImportOptions(target_kind="cloud", public_base_url=TARGET_URL), RecordingAuth()
    )
    assert any("sign-in" in e for e in rep.errors)


def test_accounts_must_be_persons_of_the_deployment(src_db):
    for kw in ({"no_sign_in": ["u_nobody"]}, {"invites": {"u_nobody": "d" * 64}}, {"password_hashes": {w.EXT: "$x"}}):
        with pytest.raises(ValueError, match="not a person"):
            b"".join(deployexport.export_deployment(src_db, export_opts(**kw), encrypted=True))


def test_no_vapid_drops_push_subscriptions(src_db):
    text = plaintext(bundle_bytes(src_db, vapid=None))
    assert "/pushSubscriptions/" not in text and '"path":"vapid/' not in text
    m = open_bundle(bundle_bytes(src_db, vapid=None)).manifest()
    assert any(d.collection == "pushSubscriptions" for f in m.families for d in f.dropped)


def test_invite_codes_are_the_home_login_codes(tmp_path):
    code, digest = authn_home.new_invite_code()
    assert len(code.replace("-", "")) == 12 and code.count("-") == 2
    db = get_db()
    db.collection("inviteCodes").document(digest).set(
        {"uid": "u1", "createdBy": "move", "createdAt": datetime.now(UTC), "expiresAt": datetime.now(UTC) + timedelta(days=1)}
    )
    assert authn_home.redeem_invite(code.lower()) == "u1"
    assert authn_home.redeem_invite(code) is None


def test_bundle_sections_must_match(tmp_path, src_db):
    lines = list(open_bundle(bundle_bytes(src_db)).lines())
    body = [line for line in lines[1:-1] if b'"section":"deployment"' not in line]
    sha = hashlib.sha256(lines[0] + b"".join(body)).hexdigest()
    forged = b"".join(
        familyexport.pack([lines[0], *body, (json.dumps({"sha256": sha}) + "\n").encode()], PASS)
    )
    with pytest.raises(CorruptFile, match="sections"):
        deployexport.plan_deployment_import(make_store("sqlite", tmp_path, "d"), open_bundle(forged), HOME)


def test_cli_export_inspect_and_dry_run(tmp_path, monkeypatch, capsys, src_db):
    monkeypatch.setenv("KPFAM_PASSPHRASE", PASS)
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://cloud.example")
    monkeypatch.setattr(deployexport, "_db", lambda: src_db)
    out = tmp_path / "x.kpdep"
    rc = deployexport.main(
        ["export", "--target-url", TARGET_URL + "/", "--target-auth", "home", "-o", str(out), "--no-history"]
    )
    cap = capsys.readouterr()
    assert rc == 0 and out.exists()
    claims = [line for line in cap.out.splitlines() if line.startswith("claim for ")]
    assert len(claims) == 5 and all(f"{TARGET_URL}/login#invite=" in line for line in claims)
    assert deployexport.main(["inspect", str(out)]) == 0
    manifest = json.loads(capsys.readouterr().out)
    assert manifest["move"]["target"]["baseUrl"] == TARGET_URL and manifest["history"] is False

    dst = make_store("sqlite", tmp_path, "d")
    monkeypatch.setattr(deployexport, "_db", lambda: dst)
    monkeypatch.setenv("PUBLIC_BASE_URL", TARGET_URL)
    monkeypatch.setenv("AUTH_BACKEND", "home")
    assert deployexport.main(["import", str(out), "--dry-run"]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True
    assert dump(dst) == {}
    # no passphrase, no export
    monkeypatch.delenv("KPFAM_PASSPHRASE")
    monkeypatch.setattr("sys.stdin.isatty", lambda: False, raising=False)
    assert deployexport.main(["export", "--target-url", TARGET_URL, "--target-auth", "home", "-o", str(tmp_path / "y")]) == 1
    assert not (tmp_path / "y").exists()


def test_cli_export_refuses_an_unfrozen_family(tmp_path, monkeypatch, capsys, src_db):
    src_db.collection("families").document(w.FA).update({"frozen": False})
    monkeypatch.setenv("KPFAM_PASSPHRASE", PASS)
    monkeypatch.setattr(deployexport, "_db", lambda: src_db)
    out = tmp_path / "z.kpdep"
    assert deployexport.main(["export", "--target-url", TARGET_URL, "--target-auth", "home", "-o", str(out)]) == 1
    assert "not frozen" in capsys.readouterr().err and not out.exists()


class FakePush:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str]] = []

    def __call__(self, **kw):
        self.sent.append((kw["subscription_info"]["endpoint"], kw["vapid_private_key"], kw["data"]))


def test_web_push_uses_the_moved_key_for_moved_subscriptions(monkeypatch):
    own_priv, _ = homeinit.vapid_keypair()
    src_priv, src_pub = homeinit.vapid_keypair()
    sender = push_webpush.WebPushSender(own_priv, "mailto:x@example.com", base_url="https://home.example/kid-pager")
    push_webpush.set_resolver(lambda host: ["93.184.216.34"])
    fake = FakePush()
    monkeypatch.setattr(push_webpush, "webpush", fake)
    subs = [
        subs_store.PushSubscription(id="a", endpoint="https://push.example/own", p256dh="p", auth="a"),
        subs_store.PushSubscription(id="b", endpoint="https://push.example/moved", p256dh="p", auth="a", vapidKid="src"),
    ]
    sender.send("u1", subs, {"url": "/chat/x"})
    assert [s[0] for s in fake.sent] == ["https://push.example/own"]  # no vapid/src key yet
    vapid_store.put_src(vapid_store.VapidKey(privateKey=src_priv, publicKey=src_pub))
    fake.sent.clear()
    sender.send("u1", subs, {"url": "/chat/x"})
    own, moved = fake.sent
    assert json.loads(own[2])["url"] == "/chat/x"
    assert moved[0] == "https://push.example/moved"
    assert json.loads(moved[2])["url"] == "https://home.example/kid-pager/chat/x"
    assert moved[1] is not own[1]


def test_a_new_subscription_is_against_the_servers_own_key():
    from app.store import users as users_store

    users_store.create_user(uid="u9", alias="nine", display_name="N", role="member", family_id="f", kind="person")
    sid = subs_store.upsert("u9", "https://push.example/e", "p", "a")
    get_db().collection("users").document("u9").collection("pushSubscriptions").document(sid).set(
        {"vapidKid": "src"}, merge=True
    )
    subs_store.upsert("u9", "https://push.example/e", "p2", "a")
    (sub,) = subs_store.list_subscriptions("u9")
    assert sub.vapidKid is None and sub.p256dh == "p2"


def _rows_by_section(data: bytes) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    current = ""
    for line in plaintext(data).splitlines():
        rec = json.loads(line)
        if rec.get("section") == "family":
            current = rec["id"]
        elif rec.get("section") == "deployment":
            current = ""
        elif "path" in rec:
            out.setdefault(current, []).append(rec["path"])
    return out


def test_rows_between_two_families_travel_once(tmp_path, src_db):
    cross_conv = f"{w.BKID}|{w.KID}"
    sections = _rows_by_section(bundle_bytes(src_db))
    everything = [p for rows in sections.values() for p in rows]
    for path in (
        f"allow/{w.KID}_{w.BKID}",
        f"allow/{w.BKID}_{w.KID}",
        f"conversations/{cross_conv}",
        "messages/m4",
        f"wireIds/w7_{w.KID}",
    ):
        assert everything.count(path) == 1, path
        assert path in sections[w.FA]  # written by the first family
    assert f"users/{w.KID}/book/{w.BKID}" in sections[w.FA]


def test_rows_between_two_families_import(tmp_path, src_db):
    dst = make_store("sqlite", tmp_path, "d")
    res = do_import(dst, bundle_bytes(src_db))
    assert res.report.ok
    got = dump(dst)
    assert got[f"allow/{w.KID}_{w.BKID}"]["familyIds"] == sorted([w.FA, w.FB])
    assert got[f"conversations/{w.BKID}|{w.KID}"]["familyIds"] == sorted([w.FA, w.FB])
    assert got["messages/m4"]["familyIds"] == sorted([w.FA, w.FB])
    assert f"wireIds/w7_{w.KID}" in got
    assert not any(d.collection in ("allow", "conversations", "messages") for d in res.report.dropped)


def test_a_row_naming_a_person_outside_the_deployment_is_dropped(src_db):
    src_db.collection("allow").document("k_outsider").set(
        {"fromUid": w.KID, "toUid": "u_outsider", "message": True, "locate": False, "familyIds": [w.FA]}
    )
    m = open_bundle(bundle_bytes(src_db)).manifest()
    fa = next(f for f in m.families if f.id == w.FA)
    assert any(d.collection == "allow" for d in fa.dropped)
    assert "allow/k_outsider" not in plaintext(bundle_bytes(src_db))


def test_contact_request_naming_a_person_in_the_other_family_is_kept(src_db):
    src_db.collection("contactRequests").document("pgr-a1_rx").set(
        {"deviceId": w.DEV, "reqId": "rx", "ownerUid": w.KID, "peerUid": w.BKID, "status": "pending"}
    )
    text = plaintext(bundle_bytes(src_db))
    assert text.count("contactRequests/pgr-a1_rx") == 1


def test_the_source_move_slot_is_not_in_the_bundle(src_db):
    rows = [json.loads(x) for x in open_bundle(bundle_bytes(src_db)).lines()]
    dev = next(r["data"] for r in rows if r.get("path") == f"devices/{w.DEV}")
    assert "pendingCfgMv" not in dev and dev["pendingBook"] == PENDING


def test_an_unencrypted_bundle_is_refused(tmp_path, src_db):
    lines = list(open_bundle(bundle_bytes(src_db)).lines())
    plain = b"".join(familyexport.pack(lines, None))
    assert plain[:2] == b"\x1f\x8b"
    with pytest.raises(CorruptFile, match="encrypted"):
        deployexport.plan_deployment_import(make_store("sqlite", tmp_path, "d"), open_bundle(plain, None), HOME)


@pytest.mark.parametrize(
    "path", ["settings/meta", "settings/move", "sessions/" + "e" * 64, "users/u_x", "vapid/own"]
)
def test_the_deployment_section_writes_only_what_the_export_writes(tmp_path, src_db, path):
    lines = list(open_bundle(bundle_bytes(src_db)).lines())
    i = lines.index(b'{"section":"deployment"}\n')
    row = (json.dumps({"path": path, "data": {"uid": w.SUP, "seqCounter": 1}}) + "\n").encode()
    body = [*lines[1:-1]]
    body.insert(i, row)
    sha = hashlib.sha256(lines[0] + b"".join(body)).hexdigest()
    forged = b"".join(
        familyexport.pack([lines[0], *body, (json.dumps({"sha256": sha}) + "\n").encode()], PASS)
    )
    dst = make_store("sqlite", tmp_path, "d")
    rep = deployexport.plan_deployment_import(dst, open_bundle(forged), HOME)
    assert not rep.ok and any(path in e for e in rep.errors)
    with pytest.raises(DeploymentImportRefused):
        do_import(dst, forged)
    assert path not in dump(dst)


def test_a_firebase_bundle_carries_no_home_credentials(src_db):
    text = plaintext(bundle_bytes(src_db, target_auth_backend="firebase"))
    assert "/credentials/" not in text
    assert "/credentials/" in plaintext(bundle_bytes(src_db))


# ---- an interrupted import -------------------------------------------------------------------

FIRST_RUN = [
    {"path": "families/default", "data": {"name": "Home", "createdBy": "setup"}},
    {"path": "users/u_first", "data": {"alias": "first", "role": "super", "kind": "person", "familyId": "default"}},
    {"path": "aliases/first", "data": {"uid": "u_first"}},
    {"path": "users/u_first/credentials/password", "data": {"kind": "password", "hash": "h"}},
]
FIRST_RUN_OPTS = DeploymentImportOptions(
    target_kind="home",
    public_base_url=TARGET_URL,
    placeholder_family_ids=["default"],
    placeholder_uids=["u_first"],
    placeholder_aliases=["first"],
)


class Crash(RuntimeError):
    pass


def _comparable(db) -> dict[str, dict]:
    """What an import leaves, without what differs run to run."""
    out = {}
    for path, data in dump(db).items():
        data = dict(data)
        if path == "settings/move":
            data.pop("importedAt", None)
        if path.startswith("settings/meta"):
            continue
        for k in ("seq", "createdAt"):
            data.pop(k, None)
        if path.startswith("fedFamilies/"):
            data.pop("privateKey", None)
            data.pop("publicKey", None)
        if path.startswith("fedLinks/"):
            data.pop("announce", None)
        out[path] = data
    return out


def _crash_at_row(monkeypatch, n: int) -> None:
    real = deployexport.sqlitestore.from_wire
    calls = {"n": 0}

    def flaky(data):
        calls["n"] += 1
        if calls["n"] == n:
            raise Crash(f"row {n}")
        return real(data)

    monkeypatch.setattr(deployexport.sqlitestore, "from_wire", flaky)


def _crash_before(monkeypatch, name: str) -> None:
    def boom(*_a, **_k):
        raise Crash(name)

    monkeypatch.setattr(deployexport, name, boom)


@pytest.mark.parametrize("point", ["first_row", "mid_rows", "before_placeholder", "before_final_record"])
def test_an_interrupted_import_is_finished_by_the_same_bundle(tmp_path, src_db, monkeypatch, point):
    data = bundle_bytes(src_db)
    ref = make_store("sqlite", tmp_path, "ref", FIRST_RUN)
    do_import(ref, data, opts=FIRST_RUN_OPTS)
    want = _comparable(ref)

    dst = make_store("sqlite", tmp_path, "d", FIRST_RUN)
    with monkeypatch.context() as m:
        if point == "first_row":
            _crash_at_row(m, 1)
        elif point == "mid_rows":
            _crash_at_row(m, 25)
        elif point == "before_placeholder":
            _crash_before(m, "_clear_placeholder")
        else:
            _crash_before(m, "TargetMoveRecord")
        with pytest.raises(Crash):
            do_import(dst, data, opts=FIRST_RUN_OPTS)
    left = dump(dst)["settings/move"]
    assert left["state"] == "importing" and left["id"] == MOVE_ID and "importedAt" not in left
    # the route offers no placeholder; the record remembers it
    res = do_import(dst, data)
    assert res.report.ok
    assert _comparable(dst) == want
    final = dump(dst)["settings/move"]
    assert final["state"] == "imported" and "placeholder" not in final


def test_a_different_bundle_is_refused_by_an_unfinished_import(tmp_path, src_db, monkeypatch):
    data = bundle_bytes(src_db)
    other = bundle_bytes(src_db, move_id="mv_aaaaaaaaaaaa")
    dst = make_store("sqlite", tmp_path, "d")
    _crash_before(monkeypatch, "_clear_placeholder")
    with pytest.raises(Crash):
        do_import(dst, data)
    monkeypatch.undo()
    before = dump(dst)
    admin = RecordingAdmin()
    with pytest.raises(DeploymentImportRefused) as ei:
        do_import(dst, other, admin)
    assert any("unfinished import of another move" in e for e in ei.value.report.errors)
    assert admin.calls == [] and dump(dst) == before


def test_a_finished_import_cannot_be_run_again(tmp_path, src_db):
    data = bundle_bytes(src_db)
    dst = make_store("sqlite", tmp_path, "d")
    do_import(dst, data)
    with pytest.raises(DeploymentImportRefused) as ei:
        do_import(dst, data)
    assert any("already has a move on record" in e for e in ei.value.report.errors)


def test_the_record_is_the_first_write_and_the_final_one_the_last(tmp_path, src_db, monkeypatch):
    dst = make_store("sqlite", tmp_path, "d")
    order: list[str] = []
    real = dst.collection

    class Spy:
        def __init__(self, inner):
            self.inner = inner

        def document(self, did):
            ref = self.inner.document(did)
            path = f"{self.inner.id}/{did}"
            outer = ref

            class Ref:
                def __getattr__(self, name):
                    return getattr(outer, name)

                def set(self, *a, **k):
                    order.append(path)
                    return outer.set(*a, **k)

                def update(self, *a, **k):
                    order.append(path)
                    return outer.update(*a, **k)

            return Ref()

        def __getattr__(self, name):
            return getattr(self.inner, name)

    monkeypatch.setattr(dst, "collection", lambda name: Spy(real(name)))
    do_import(dst, bundle_bytes(src_db))
    assert order[0] == "settings/move" and order[-1] == "settings/move"
    assert order.count("settings/move") == 2


def test_a_repeated_import_keeps_the_federation_key_the_first_run_minted(tmp_path, src_db, monkeypatch):
    data = bundle_bytes(src_db)
    dst = make_store("sqlite", tmp_path, "d")
    _crash_before(monkeypatch, "_clear_placeholder")
    with pytest.raises(Crash):
        do_import(dst, data)
    monkeypatch.undo()
    first = dump(dst)[f"fedFamilies/{w.FA}"]
    do_import(dst, data)
    again = dump(dst)[f"fedFamilies/{w.FA}"]
    assert again["privateKey"] == first["privateKey"] and again["publicKey"] == first["publicKey"]
    assert again["epoch"] == 2 and again["prevPrivateKey"] == "fed-private"


def test_a_placeholder_that_shares_an_id_with_the_bundle_is_not_removed_after_it(tmp_path, src_db):
    dst = make_store(
        "sqlite",
        tmp_path,
        "d",
        [{"path": f"families/{w.FA}", "data": {"name": "Home", "createdBy": "setup"}}],
    )
    opts = DeploymentImportOptions(
        target_kind="home", public_base_url=TARGET_URL, placeholder_family_ids=[w.FA]
    )
    do_import(dst, bundle_bytes(src_db), opts=opts)
    fam = dump(dst)[f"families/{w.FA}"]
    assert fam["importing"] is False and fam.get("createdBy") != "setup"


def test_a_second_move_keeps_the_key_each_subscription_was_made_against(tmp_path, src_db):
    a_priv, a_pub = homeinit.vapid_keypair()
    a_key = vapid_store.VapidKey(privateKey=a_priv, publicKey=a_pub)
    # this server was itself a target once: s2 was made against the first server's key
    src_db.collection("vapid").document("src").set(a_key.model_dump())
    src_db.collection("users").document(w.MOM).collection("pushSubscriptions").document("s2").set(
        {"vapidKid": "src"}, merge=True
    )
    dst = make_store("sqlite", tmp_path, "d")
    do_import(dst, bundle_bytes(src_db))
    got = dump(dst)
    carried = vapid_store.kid_for(a_pub)
    assert got[f"users/{w.MOM}/pushSubscriptions/s1"]["vapidKid"] == "src"
    assert got[f"users/{w.MOM}/pushSubscriptions/s2"]["vapidKid"] == carried
    assert got["vapid/src"] == SRC_VAPID.model_dump()
    assert got[f"vapid/{carried}"] == a_key.model_dump()


def test_web_push_signs_each_subscription_with_the_key_it_names(monkeypatch):
    own_priv, _ = homeinit.vapid_keypair()
    a_priv, a_pub = homeinit.vapid_keypair()
    b_priv, b_pub = homeinit.vapid_keypair()
    sender = push_webpush.WebPushSender(own_priv, "mailto:x@example.com")
    push_webpush.set_resolver(lambda host: ["93.184.216.34"])
    fake = FakePush()
    monkeypatch.setattr(push_webpush, "webpush", fake)
    kid_a = vapid_store.kid_for(a_pub)
    get_db().collection("vapid").document("src").set({"privateKey": b_priv, "publicKey": b_pub})
    get_db().collection("vapid").document(kid_a).set({"privateKey": a_priv, "publicKey": a_pub})
    subs = [
        subs_store.PushSubscription(id="b", endpoint="https://push.example/b", p256dh="p", auth="a", vapidKid="src"),
        subs_store.PushSubscription(id="a", endpoint="https://push.example/a", p256dh="p", auth="a", vapidKid=kid_a),
    ]
    sender.send("u1", subs, {"url": "/x"})
    keys_used = {url: key for url, key, _ in fake.sent}
    assert set(keys_used) == {"https://push.example/a", "https://push.example/b"}
    assert keys_used["https://push.example/a"] is not keys_used["https://push.example/b"]
