"""`python -m app.homebackup`: the home stack's `backup` profile."""

from __future__ import annotations

import sqlite3
import tarfile
from datetime import UTC, datetime
from pathlib import Path

from app import homebackup


def _data(tmp_path: Path) -> Path:
    data = tmp_path / "data"
    (data / "relay").mkdir(parents=True)
    (data / "certs" / "ca").mkdir(parents=True)
    (data / "mosquitto").mkdir()
    (data / "secrets.env").write_text("WEBHOOK_KEY=k\n")
    (data / "certs" / "ca" / "ca.crt").write_text("CA")
    (data / "mosquitto" / "dynamic-security.json").write_text("{}")
    (data / "relay.env").write_text("PUBLIC_BASE_URL=https://x\n")
    return data


def test_backup_copies_a_live_wal_database_and_tars_the_keys(tmp_path: Path):
    data = _data(tmp_path)
    live = sqlite3.connect(data / "relay" / "pager.sqlite")
    live.execute("PRAGMA journal_mode=WAL")
    live.execute("CREATE TABLE docs(path TEXT PRIMARY KEY, data TEXT)")
    live.execute("INSERT INTO docs VALUES ('users/u1', '{}')")
    live.commit()  # still open: the write sits in the -wal file
    try:
        db_out, tar_out = homebackup.backup(data, datetime(2026, 10, 31, 12, 0, 5, tzinfo=UTC))
    finally:
        live.close()
    assert db_out == data / "backups" / "2026-10-31T120005Z.sqlite"
    copy = sqlite3.connect(db_out)
    assert copy.execute("SELECT path FROM docs").fetchall() == [("users/u1",)]
    copy.close()
    with tarfile.open(tar_out) as tar:
        names = set(tar.getnames())
    assert {"secrets.env", "certs/ca/ca.crt", "mosquitto/dynamic-security.json"} <= names
    assert "relay.env" not in names and not any(n.startswith("relay") for n in names)
    assert db_out.stat().st_mode & 0o777 == 0o600 and tar_out.stat().st_mode & 0o777 == 0o600


def test_main_without_a_database_fails(tmp_path: Path, capsys):
    assert homebackup.main(["--data", str(_data(tmp_path))]) == 1
    assert "no database" in capsys.readouterr().err


def test_restore_puts_the_backup_back_and_keeps_the_replaced_db(tmp_path: Path):
    data = _data(tmp_path)
    db = data / "relay" / "pager.sqlite"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE docs(path TEXT)")
    conn.execute("INSERT INTO docs VALUES ('old')")
    conn.commit()
    conn.close()
    stamp = datetime(2026, 10, 31, 12, 0, 5, tzinfo=UTC)
    homebackup.backup(data, stamp)
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO docs VALUES ('after-backup')")
    conn.commit()
    conn.close()
    (data / "secrets.env").write_text("WEBHOOK_KEY=changed\n")

    assert homebackup.main(["--data", str(data), "restore", "2026-10-31T120005Z"]) == 0
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT path FROM docs").fetchall() == [("old",)]
    conn.close()
    assert (data / "secrets.env").read_text() == "WEBHOOK_KEY=k\n"
    (aside,) = (data / "backups").glob("pre-restore-*")
    conn = sqlite3.connect(aside / "pager.sqlite")
    assert len(conn.execute("SELECT path FROM docs").fetchall()) == 2
    conn.close()


def test_restore_refuses_while_the_relay_holds_the_db(tmp_path: Path, capsys):
    import fcntl

    data = _data(tmp_path)
    sqlite3.connect(data / "relay" / "pager.sqlite").close()
    homebackup.backup(data, datetime(2026, 10, 31, tzinfo=UTC))
    with open(data / "relay" / "pager.sqlite.lock", "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert homebackup.main(["--data", str(data), "restore", "2026-10-31T000000Z"]) == 1
    assert "docker compose down" in capsys.readouterr().err
    assert homebackup.main(["--data", str(data), "restore", "1999-01-01T000000Z"]) == 1


def test_backup_keeps_only_the_newest_archives(tmp_path: Path):
    data = _data(tmp_path)
    db = sqlite3.connect(data / "relay" / "pager.sqlite")
    db.execute("CREATE TABLE docs(path TEXT PRIMARY KEY, data TEXT)")
    db.commit()
    db.close()
    for day in range(1, 6):
        homebackup.backup(data, datetime(2026, 10, day, 12, 0, 0, tzinfo=UTC), keep=3)
    kept = sorted(p.name for p in (data / "backups").iterdir())
    assert kept == [
        f"2026-10-0{d}T120000Z.{ext}" for d in (3, 4, 5) for ext in ("sqlite", "tar.gz")
    ]


def test_prune_leaves_pre_restore_dirs_and_keep_zero_keeps_everything(tmp_path: Path):
    data = _data(tmp_path)
    out = data / "backups"
    out.mkdir()
    (out / "pre-restore-2026-10-01T000000Z").mkdir()
    for day in (1, 2, 3):
        (out / f"2026-10-0{day}T000000Z.sqlite").write_text("x")
        (out / f"2026-10-0{day}T000000Z.tar.gz").write_text("x")
    assert homebackup.prune(data, 0) == []
    assert homebackup.prune(data, 1) == ["2026-10-01T000000Z", "2026-10-02T000000Z"]
    assert (out / "pre-restore-2026-10-01T000000Z").is_dir()
    assert sorted(p.name for p in out.iterdir() if p.is_file()) == [
        "2026-10-03T000000Z.sqlite",
        "2026-10-03T000000Z.tar.gz",
    ]


def test_main_keep_flag(tmp_path: Path):
    data = _data(tmp_path)
    db = sqlite3.connect(data / "relay" / "pager.sqlite")
    db.execute("CREATE TABLE docs(path TEXT PRIMARY KEY, data TEXT)")
    db.commit()
    db.close()
    out = data / "backups"
    out.mkdir()
    (out / "2020-01-01T000000Z.sqlite").write_text("x")
    assert homebackup.main(["--data", str(data), "--keep", "1"]) == 0
    assert not (out / "2020-01-01T000000Z.sqlite").exists()
