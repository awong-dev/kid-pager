"""`/api/admin/families/*` move routes and the `python -m app.familyexport` CLI."""

from __future__ import annotations

import io
import json
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from firebase_admin import auth as fb_auth

from app import dbexport, familyexport
from app.config import auth_backend
from app.db.firestore import get_db
from app.familyexport import KpfamFile
from app.main import create_app
from app.store import families as families_store
from app.store import users as users_store
from tests import familymove_world as w
from tests.fake_transport import FakeBrokerClient
from tests.firebase_test_utils import auth_header
from tests.test_admin import make_settings

PW = {"X-Export-Passphrase": "correct horse"}


@pytest.fixture
def client() -> Iterator[TestClient]:
    app = create_app(settings=make_settings(), broker_client=FakeBrokerClient())
    with TestClient(app) as c:
        yield c


# the autouse fixture below stubs these for the import under test; the test's
# own accounts must still be real so the Auth emulator mints the role claim
_REAL: dict = {}


def _account(uid: str, role: str, fam: str) -> dict[str, str]:
    _REAL["create"](uid=uid, email=f"{uid}@example.com")
    users_store.create_user(
        uid=uid, alias=uid, display_name=uid, role=role, family_id=fam
    )  # type: ignore[arg-type]
    _REAL["claims"](uid, {"role": role, "fam": fam})
    return auth_header(uid)


@pytest.fixture
def super_h() -> dict[str, str]:
    families_store.create_family(name="Home", created_by="r", family_id="default")
    return _account("mv-super", "super", "default")


def _bundle_bytes(tmp_path, passphrase="correct horse") -> bytes:
    db = w.open_store(tmp_path / "src.sqlite")
    buf = io.BytesIO()
    familyexport.write_kpfam(
        db, w.FA, buf, passphrase=passphrase, opts=familyexport.ExportOptions(backup=True)
    )
    db.close()
    return buf.getvalue()


def _import_params() -> dict:
    # the target is a home server or the cloud depending on how the suite runs
    if auth_backend() == "home":
        return {}
    return {"email": ["moma=mom@example.com", "kida=-"]}


@pytest.fixture(autouse=True)
def _no_real_firebase(monkeypatch):
    _REAL["create"] = fb_auth.create_user
    _REAL["claims"] = fb_auth.set_custom_user_claims
    monkeypatch.setattr(fb_auth, "create_user", lambda **kw: None)
    monkeypatch.setattr(fb_auth, "set_custom_user_claims", lambda uid, c: None)


def _all_rows() -> dict[str, dict]:
    db = get_db()
    recs = (
        dbexport.export_sqlite(db._path)
        if hasattr(db, "_path")
        else dbexport.export_firestore(db)
    )
    # the write rate limiter's own counter is the only thing a dry run may touch
    return {
        r["path"]: r["data"] for r in recs if not r["path"].startswith("rateLimits/")
    }


def test_non_super_gets_403(client: TestClient):
    fam = families_store.create_family(name="X", created_by="r").id
    admin_h = _account("mv-admin", "admin", fam)
    assert (
        client.get(
            f"/api/admin/families/{fam}/export", headers=admin_h | PW
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/api/admin/families/import?dryRun=1", content=b"x", headers=admin_h | PW
        ).status_code
        == 403
    )
    assert (
        client.post(f"/api/admin/families/{fam}/freeze", headers=admin_h).status_code
        == 403
    )


def test_export_409_unfrozen_then_streams_after_freeze(client: TestClient, super_h):
    fam = families_store.create_family(name="Mover", created_by="r").id
    users_store.create_user(
        uid="mv-kid", alias="mvkid", display_name="Kid", family_id=fam
    )
    url = f"/api/admin/families/{fam}/export"
    assert client.get(url, headers=super_h | PW).status_code == 409
    assert (
        client.get(url, headers=super_h).status_code == 400
    )  # a passphrase is required
    assert (
        client.get("/api/admin/families/nope/export", headers=super_h | PW).status_code
        == 404
    )

    assert (
        client.post(f"/api/admin/families/{fam}/freeze", headers=super_h).status_code
        == 200
    )
    r = client.get(url, headers=super_h | PW)
    assert r.status_code == 200
    assert r.headers["content-disposition"].startswith(f'attachment; filename="{fam}-')
    f = KpfamFile(io.BytesIO(r.content), "correct horse")
    f.verify()
    assert f.manifest().family.id == fam and f.manifest().counts["users"] == 1
    assert "frozen" not in json.dumps(
        [row for row in f.rows() if row["path"] == f"families/{fam}"]
    )
    # a backup of a live family is allowed explicitly
    client.post(f"/api/admin/families/{fam}/unfreeze", headers=super_h)
    assert client.get(url, headers=super_h | PW).status_code == 409
    backup_url = f"/api/admin/families/{fam}/backup"
    assert client.get(backup_url, headers=super_h | PW).status_code == 200
    assert client.get(backup_url + "?history=0", headers=super_h).status_code == 200  # no passphrase


def test_import_dry_run_reports_and_writes_nothing(
    client: TestClient, super_h, tmp_path
):
    data = _bundle_bytes(tmp_path)
    before = _all_rows()
    r = client.post(
        "/api/admin/families/import",
        params={"dryRun": 1} | _import_params(),
        content=data,
        headers=super_h | PW,
    )
    assert r.status_code == 200, r.text
    rep = r.json()
    assert (
        rep["ok"] is True
        and rep["targetFamilyId"] == w.FA
        and rep["counts"]["users"] == 4
    )
    assert _all_rows() == before


def test_import_applies_and_refuses_a_repeat(client: TestClient, super_h, tmp_path):
    data = _bundle_bytes(tmp_path)
    url = "/api/admin/families/import"
    r = client.post(url, params=_import_params(), content=data, headers=super_h | PW)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["report"]["ok"] and families_store.get_family(w.FA).importing is False
    if auth_backend() == "home":
        assert sorted(i["alias"] for i in body["invites"]) == ["supa"]
    again = client.post(
        url, params=_import_params(), content=data, headers=super_h | PW
    )
    assert again.status_code == 409
    assert {c["kind"] for c in again.json()["detail"]["collisions"]} >= {
        "family",
        "uid",
    }
    renamed = client.post(
        url,
        params={"dryRun": 1, "as": "fa9", "alias": ["kida=kida2"]} | _import_params(),
        content=data,
        headers=super_h | PW,
    )
    assert (
        renamed.status_code == 200 and renamed.json()["ok"] is False
    )  # uids still collide


def test_import_bad_inputs(client: TestClient, super_h, tmp_path):
    data = _bundle_bytes(tmp_path)
    url = "/api/admin/families/import"
    assert (
        client.post(
            url, content=data, headers=super_h | {"X-Export-Passphrase": "nope"}
        ).status_code
        == 400
    )
    assert (
        client.post(url, content=data, headers=super_h).status_code == 400
    )  # encrypted, no passphrase
    assert client.post(url, content=b"garbage", headers=super_h | PW).status_code == 400
    assert (
        client.post(
            url, params={"alias": "oops"}, content=data, headers=super_h | PW
        ).status_code
        == 422
    )


def test_cli_inspect_prints_the_manifest(tmp_path, capsys):
    path = tmp_path / "f.kpfam"
    path.write_bytes(_bundle_bytes(tmp_path, passphrase="pw1"))
    pf = tmp_path / "pw"
    pf.write_text("pw1\n")
    assert familyexport.main(["inspect", str(path), "--passphrase-file", str(pf)]) == 0
    m = json.loads(capsys.readouterr().out)
    assert m["kpfam"] == 1 and m["family"] == {
        "id": w.FA,
        "name": "Alpha",
        "idSeed": w.FA,
    }
    assert familyexport.main(["inspect", str(path)]) == 1  # encrypted, no passphrase
    assert "passphrase" in capsys.readouterr().err


def test_cli_export_import_round_trip(tmp_path, monkeypatch, capsys):
    src = w.open_store(tmp_path / "s.sqlite")
    dst = w.open_store(tmp_path / "d.sqlite", [])
    current = {"db": src}
    monkeypatch.setattr(familyexport, "_db", lambda: current["db"])
    pf = tmp_path / "pw"
    pf.write_text("s3cret\n")
    out = tmp_path / "out.kpfam"

    # an unfrozen family needs `backup`; a move needs a passphrase (or --no-passphrase)
    src.collection("families").document(w.FA).update({"frozen": False})
    assert (
        familyexport.main(
            ["export", w.FA, "-o", str(out), "--passphrase-file", str(pf)]
        )
        == 1
    )
    assert "not frozen" in capsys.readouterr().err
    monkeypatch.delenv("KPFAM_PASSPHRASE", raising=False)
    assert familyexport.main(["export", w.FA, "-o", str(out), "--passphrase-file", str(pf)]) == 1
    assert (
        familyexport.main(
            ["backup", w.FA, "-o", str(out), "--passphrase-file", str(pf)]
        )
        == 0
    )
    assert out.read_bytes().startswith(familyexport.MAGIC)

    current["db"] = dst
    who = (
        ["--super", "moma"]
        if auth_backend() == "home"
        else ["--email", "moma=mom@example.com", "--email", "kida=-"]
    )
    args = ["import", str(out), "--passphrase-file", str(pf), *who]
    assert familyexport.main([*args, "--dry-run"]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True
    assert dbexport.export_sqlite(dst._path) is not None and not list(
        dbexport.export_sqlite(dst._path)
    )
    assert familyexport.main(args) == 0
    text = capsys.readouterr().out
    if auth_backend() == "home":
        assert "invite for supa:" in text
    assert any(r["path"] == f"users/{w.KID}" for r in dbexport.export_sqlite(dst._path))
    # a second import collides: exit 1 with the report on stdout
    assert familyexport.main(args) == 1
    assert '"collisions"' in capsys.readouterr().out

    plain = tmp_path / "plain.kpfam"
    current["db"] = src
    assert (
        familyexport.main(
            [
                "backup",
                w.FA,
                "-o",
                str(plain),
                "--no-passphrase",
                "--no-history",
            ]
        )
        == 0
    )
    assert "NOT be encrypted" in capsys.readouterr().err
    assert familyexport.main(["inspect", str(plain)]) == 0
    assert json.loads(capsys.readouterr().out)["history"] is False


@pytest.mark.parametrize(
    "backend,kind", [("firebase", "cloud"), ("home", "home"), ("", "cloud")]
)
def test_manifest_source_kind_is_one_rule(monkeypatch, backend, kind):
    monkeypatch.setenv("AUTH_BACKEND", backend)
    assert familyexport.source_kind() == kind


@pytest.fixture
def cloud_target(monkeypatch):
    """Make the import target a cloud server whatever backend the suite runs; Firebase is faked."""
    monkeypatch.setattr(familyexport, "auth_backend", lambda: "cloud")

    def no_such(email):
        raise fb_auth.UserNotFoundError("none", None, None)

    monkeypatch.setattr(fb_auth, "get_user_by_email", no_such)


def test_import_http_email_and_admin_email_synonym(
    client: TestClient, super_h, tmp_path, cloud_target
):
    data = _bundle_bytes(tmp_path)
    url = "/api/admin/families/import"
    base = {"dryRun": 1}
    r = client.post(url, params=base, content=data, headers=super_h | PW)
    assert r.status_code == 200 and r.json()["ok"] is False
    rep = r.json()
    assert rep["emailsMissing"] == ["kida"]
    assert [p["alias"] for p in rep["people"]] == ["kida", "moma", "supa"]
    r = client.post(
        url,
        params=base | {"email": ["moma=mom@example.com", "kida=-"]},
        content=data,
        headers=super_h | PW,
    )
    assert r.json()["ok"] is True, r.text
    # adminEmail is a synonym; `email` wins on a clash
    r = client.post(
        url,
        params=base
        | {
            "adminEmail": ["moma=old@example.com", "kida=-"],
            "email": ["moma=new@example.com"],
        },
        content=data,
        headers=super_h | PW,
    )
    mom = next(p for p in r.json()["people"] if p["alias"] == "moma")
    assert r.json()["ok"] is True and mom["email"] == "new@example.com"
    bad = client.post(
        url,
        params=base | {"email": ["moma=nope", "kida=-"]},
        content=data,
        headers=super_h | PW,
    )
    assert bad.json()["emailsInvalid"] == ["moma"]
    # a real run refuses with the report
    refused = client.post(url, content=data, headers=super_h | PW)
    assert refused.status_code == 409
    assert refused.json()["detail"]["emailsMissing"] == ["kida"]


def test_cli_cloud_template_emails_file_and_override(
    tmp_path, monkeypatch, capsys, cloud_target
):
    src = w.open_store(tmp_path / "s.sqlite")
    dst = w.open_store(tmp_path / "d.sqlite", [])
    current = {"db": src}
    monkeypatch.setattr(familyexport, "_db", lambda: current["db"])
    pf = tmp_path / "pw"
    pf.write_text("s3cret\n")
    out = tmp_path / "out.kpfam"
    assert (
        familyexport.main(
            ["backup", w.FA, "-o", str(out), "--passphrase-file", str(pf)]
        )
        == 0
    )
    capsys.readouterr()
    pwa = ["--passphrase-file", str(pf)]
    assert familyexport.main(["inspect", str(out), "--template", *pwa]) == 0
    template = capsys.readouterr().out
    assert "kida," in template.splitlines()

    current["db"] = dst
    args = ["import", str(out), *pwa, "--dry-run"]
    assert familyexport.main(args) == 1  # no answers
    cap = capsys.readouterr()
    assert json.loads(cap.out)["emailsMissing"] == ["kida"]
    assert "kida," in cap.err

    emails = tmp_path / "emails.txt"
    emails.write_text("# answers\nmoma,mom@example.com\n@kida = -\n")
    assert familyexport.main([*args, "--emails-file", str(emails)]) == 0
    capsys.readouterr()
    # --email overrides the file
    assert (
        familyexport.main(
            [*args, "--emails-file", str(emails), "--email", "kida=kid@example.com"]
        )
        == 0
    )
    people = {p["alias"]: p for p in json.loads(capsys.readouterr().out)["people"]}
    assert people["kida"]["email"] == "kid@example.com"
    emails.write_text("moma,mom@example.com\nbroken\n")
    assert familyexport.main([*args, "--emails-file", str(emails)]) == 1
    assert "line 2" in capsys.readouterr().err
