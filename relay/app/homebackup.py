"""`python -m app.homebackup`: `<stamp>.sqlite` (online backup) and `<stamp>.tar.gz` (secrets, certs,
broker state) under `data/backups/`; keeps the newest 7; `restore <stamp>` puts both back."""

from __future__ import annotations

import argparse
import fcntl
import shutil
import sqlite3
import sys
import tarfile
from datetime import UTC, datetime
from pathlib import Path

TAR_MEMBERS = ("secrets.env", "certs", "mosquitto", "emqx")
    stamp = (now or datetime.now(UTC)).strftime("%Y-%m-%dT%H%M%SZ")
    out = data / "backups"
    out.mkdir(parents=True, exist_ok=True)
    db_path = data / "relay" / "pager.sqlite"
    if not db_path.is_file():
        raise FileNotFoundError(f"no database at {db_path}")
    db_out = out / f"{stamp}.sqlite"
    src = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    dst = sqlite3.connect(db_out)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    db_out.chmod(0o600)
    tar_out = out / f"{stamp}.tar.gz"
    with tarfile.open(tar_out, "w:gz") as tar:
        for name in TAR_MEMBERS:
            if (data / name).exists():
                tar.add(data / name, arcname=name)
    tar_out.chmod(0o600)
    return db_out, tar_out


class RelayRunning(RuntimeError):
    pass


def restore(data: Path, stamp: str, now: datetime | None = None) -> Path:
    """Puts backup `stamp` back; returns where the replaced database went."""
    src_db = data / "backups" / f"{stamp}.sqlite"
    src_tar = data / "backups" / f"{stamp}.tar.gz"
    for f in (src_db, src_tar):
        if not f.is_file():
            raise FileNotFoundError(f"no backup file {f}")
    relay = data / "relay"
    relay.mkdir(parents=True, exist_ok=True)
    # The relay holds this lock for as long as it has the database open.
    with open(relay / "pager.sqlite.lock", "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RelayRunning("the relay is running; run `docker compose down` first") from exc
        aside = data / "backups" / f"pre-restore-{(now or datetime.now(UTC)):%Y-%m-%dT%H%M%SZ}"
        aside.mkdir(parents=True, exist_ok=True)
        for suffix in ("", "-wal", "-shm"):
            cur = relay / f"pager.sqlite{suffix}"
            if cur.exists():
                shutil.move(cur, aside / cur.name)
        shutil.copyfile(src_db, relay / "pager.sqlite")
        with tarfile.open(src_tar) as tar:
            # Our own archive: keep owners (mosquitto/ belongs to the broker).
            tar.extractall(data, filter="tar")
    return aside


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="homebackup", description=__doc__.split("\n\n")[0])
    parser.add_argument("--data", type=Path, default=Path("/data"))
    sub = parser.add_subparsers(dest="command")
    p_restore = sub.add_parser("restore", help="put backup <stamp> back (stack stopped)")
    p_restore.add_argument("stamp", help="e.g. 2026-10-31T120005Z, from data/backups/<stamp>.sqlite")
    args = parser.parse_args(argv)
    try:
        if args.command == "restore":
            aside = restore(args.data, args.stamp)
            print(f"homebackup: restored {args.stamp}; the previous database is in {aside}")
            return 0
    except (OSError, sqlite3.Error, RelayRunning) as exc:
        print(f"homebackup: {exc}", file=sys.stderr)
        return 1
    print(f"homebackup: {db_out}")
    print(f"homebackup: {tar_out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
