"""`python -m app.dbexport export|import|convert`: move a database as newline-delimited JSON.
Timestamps are `{"$ts": <microseconds UTC>}`, bytes `{"$b64": ...}`; `<file>` may be `-`."""

from __future__ import annotations

import argparse
import json
import sqlite3
import struct
import sys
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any, TextIO

from app.config import Settings
from app.db import sqlitestore

Record = dict[str, Any]

BATCH = 400


def write_ndjson(out: TextIO, records: Iterable[Record]) -> int:
    n = 0
    for rec in records:
        out.write(json.dumps(rec, separators=(",", ":"), ensure_ascii=False, allow_nan=False))
        out.write("\n")
        n += 1
    return n


def read_ndjson(src: TextIO) -> Iterator[Record]:
    for lineno, line in enumerate(src, 1):
        if not line.strip():
            continue
        rec = json.loads(line)
        if not (
            isinstance(rec, dict)
            and isinstance(rec.get("path"), str)
            and isinstance(rec.get("data"), dict)
            and _is_doc_path(rec["path"])
        ):
            raise ValueError(f"line {lineno}: not a {{path, data}} document record")
        yield rec


def _is_doc_path(path: str) -> bool:
    segs = path.split("/")
    return len(segs) % 2 == 0 and all(segs)


def export_sqlite(path: str) -> Iterator[Record]:
    if not Path(path).is_file():
        raise FileNotFoundError(f"no database at {path}")
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        # The stored text is already the record encoding.
        for doc_path, data in conn.execute("SELECT path, data FROM docs ORDER BY path"):
            yield {"path": doc_path, "data": json.loads(data)}
    finally:
        conn.close()


def export_firestore(db: Any) -> Iterator[Record]:
    out: list[Record] = []

    def walk(collections: Iterable[Any]) -> None:
        for col in collections:
            for ref in col.list_documents():
                snap = ref.get()
                if snap.exists:
                    out.append({"path": ref.path, "data": sqlitestore.to_wire(snap.to_dict())})
                walk(ref.collections())

    walk(db.collections())
    yield from sorted(out, key=lambda r: r["path"])


class NotEmpty(RuntimeError):
    pass


def _is_empty(db: Any) -> bool:
    if isinstance(db, sqlitestore.Client):
        return db.document_count() == 0
    return next(iter(db.collections()), None) is None


def import_records(db: Any, records: Iterable[Record], *, merge: bool = False) -> int:
    if not merge and not _is_empty(db):
        raise NotEmpty("the database already holds documents; pass --merge to write over it")
    n = 0
    batch = db.batch()
    pending = 0
    for rec in records:
        parent, _, doc_id = rec["path"].rpartition("/")
        batch.set(db.collection(parent).document(doc_id), sqlitestore.from_wire(rec["data"]))
        pending += 1
        n += 1
        if pending == BATCH:
            batch.commit()
            batch, pending = db.batch(), 0
    if pending:
        batch.commit()
    return n


# A `gcloud firestore export` `output-N` is a LevelDB log: 32 KiB blocks of
# `crc32c(4, masked) | length(2, LE) | type(1) | payload`, 1 FULL 2 FIRST 3 MIDDLE 4 LAST.
_BLOCK = 32768
_MEANING_GD_WHEN = 7
_MEANING_GEO = 9
_MEANING_BLOB = 14
_MEANING_BYTESTRING = 16
_MEANING_ENTITY = 19
_MEANING_EMPTY_LIST = 24


class ConvertError(ValueError):
    pass


def _log_records(data: bytes) -> Iterator[bytes]:
    import google_crc32c

    pos, parts = 0, bytearray()
    while pos < len(data):
        left = _BLOCK - pos % _BLOCK
        if left < 7:
            pos += left
            continue
        crc, length, rtype = struct.unpack_from("<IHB", data, pos)
        payload = data[pos + 7 : pos + 7 + length]
        pos += 7 + length
        if rtype == 0:  # pre-allocated zeros
            continue
        raw = google_crc32c.value(bytes([rtype]) + payload)
        masked = (((raw >> 15) | (raw << 17)) + 0xA282EAD8) & 0xFFFFFFFF
        if masked != crc or len(payload) != length:
            raise ConvertError("corrupt export file (record checksum)")
        if rtype == 1:
            yield bytes(payload)
        elif rtype == 2:
            parts = bytearray(payload)
        elif rtype == 3:
            parts += payload
        elif rtype == 4:
            parts += payload
            yield bytes(parts)
            parts = bytearray()
        else:
            raise ConvertError(f"corrupt export file (record type {rtype})")


def _varint(buf: bytes, pos: int) -> tuple[int, int]:
    shift = result = 0
    while True:
        b = buf[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not b & 0x80:
            return result, pos
        shift += 7


def _fields(buf: bytes, pos: int = 0, end: int | None = None, group: int | None = None):
    """(field, wire type, value, next pos); a group's value is its (start, end) span."""
    end = len(buf) if end is None else end
    while pos < end:
        key, pos = _varint(buf, pos)
        fnum, wt = key >> 3, key & 7
        if wt == 0:
            val, pos = _varint(buf, pos)
        elif wt == 1:
            val, pos = buf[pos : pos + 8], pos + 8
        elif wt == 2:
            n, pos = _varint(buf, pos)
            val, pos = buf[pos : pos + n], pos + n
        elif wt == 5:
            val, pos = buf[pos : pos + 4], pos + 4
        elif wt == 3:
            start = pos
            depth = 1
            stop = pos
            while depth:
                stop = pos
                k, pos = _varint(buf, pos)
                w = k & 7
                if w == 3:
                    depth += 1
                elif w == 4:
                    depth -= 1
                elif w == 0:
                    _, pos = _varint(buf, pos)
                elif w == 1:
                    pos += 8
                elif w == 2:
                    n, pos = _varint(buf, pos)
                    pos += n
                elif w == 5:
                    pos += 4
            val = (start, stop)
        elif wt == 4:
            if group is not None and fnum == group:
                return
            raise ConvertError("unbalanced group")
        else:
            raise ConvertError(f"unknown wire type {wt}")
        yield fnum, wt, val


def _signed(v: int) -> int:
    return v - (1 << 64) if v >= 1 << 63 else v


def _key_path(ref: bytes) -> str:
    segs: list[str] = []
    for fnum, wt, val in _fields(ref):
        if fnum != 14 or wt != 2:
            continue
        for f2, w2, span in _fields(val):
            if f2 != 1 or w2 != 3:
                continue
            kind, name, num = None, None, None
            for f3, _w3, v3 in _fields(val, span[0], span[1]):
                if f3 == 2:
                    kind = v3.decode()
                elif f3 == 4:
                    name = v3.decode()
                elif f3 == 3:
                    num = _signed(v3)
            if kind is None:
                raise ConvertError("key element without a kind")
            segs += [kind, name if name is not None else f"__id{num}__"]
    return "/".join(segs)


def _value(meaning: int, pv: bytes, field: str) -> Any:
    got: dict[int, Any] = {}
    for fnum, _wt, val in _fields(pv):
        got[fnum] = val
    if 5 in got or meaning == _MEANING_GEO:
        raise ConvertError(f"field {field!r}: geo points are not supported")
    if 12 in got:
        raise ConvertError(f"field {field!r}: document references are not supported")
    if 8 in got:
        raise ConvertError(f"field {field!r}: user values are not supported")
    if meaning == _MEANING_GD_WHEN and 1 in got:
        return {"$ts": _signed(got[1])}
    if 1 in got:
        return _signed(got[1])
    if 2 in got:
        return bool(got[2])
    if 4 in got:
        return struct.unpack("<d", got[4])[0]
    if 3 in got:
        raw = got[3]
        if meaning == _MEANING_ENTITY:
            return _entity(raw)[1]
        if meaning in (_MEANING_BLOB, _MEANING_BYTESTRING):
            import base64

            return {"$b64": base64.b64encode(raw).decode("ascii")}
        return raw.decode("utf-8")
    return None


def _entity(buf: bytes) -> tuple[str, dict[str, Any]]:
    """(document path, wire-form data) of one `EntityProto`."""
    path = ""
    data: dict[str, Any] = {}
    arrays: set[str] = set()
    for fnum, wt, val in _fields(buf):
        if fnum == 13 and wt == 2:
            path = _key_path(val)
        elif fnum in (14, 15) and wt == 2:
            meaning, name, multiple, pv = 0, None, False, b""
            for f2, _w2, v2 in _fields(val):
                if f2 == 1:
                    meaning = v2
                elif f2 == 3:
                    name = v2.decode("utf-8")
                elif f2 == 4:
                    multiple = bool(v2)
                elif f2 == 5:
                    pv = v2
            if name is None:
                raise ConvertError("property without a name")
            if meaning == _MEANING_EMPTY_LIST:
                data[name] = []
                arrays.add(name)
                continue
            value = _value(meaning, pv, name)
            if multiple:
                if name not in arrays:
                    data[name] = []
                    arrays.add(name)
                data[name].append(value)
            else:
                data[name] = value
    return path, data


def convert_export(export_dir: str | Path) -> Iterator[Record]:
    """Records from a `gcloud firestore export` directory, in path order."""
    root = Path(export_dir)
    files = sorted(p for p in root.rglob("output-*") if p.is_file())
    if not files:
        raise ConvertError(f"no output-* files under {root}")
    out: list[Record] = []
    for f in files:
        for rec in _log_records(f.read_bytes()):
            path, data = _entity(rec)
            if not _is_doc_path(path):
                raise ConvertError(f"{f.name}: entity without a document key")
            out.append({"path": path, "data": data})
    yield from sorted(out, key=lambda r: r["path"])


def _open_out(name: str) -> TextIO:
    return sys.stdout if name == "-" else open(name, "w", encoding="utf-8")


def _open_in(name: str) -> TextIO:
    return sys.stdin if name == "-" else open(name, encoding="utf-8")


def _db() -> Any:
    from app.db.firestore import get_db

    return get_db()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="dbexport", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p_exp = sub.add_parser("export", help="write the configured database to an NDJSON file")
    p_exp.add_argument("file")
    p_imp = sub.add_parser("import", help="write an NDJSON file into the configured database")
    p_imp.add_argument("file")
    p_imp.add_argument("--merge", action="store_true", help="write over a non-empty database")
    p_conv = sub.add_parser("convert", help="gcloud firestore export directory -> NDJSON file")
    p_conv.add_argument("export_dir")
    p_conv.add_argument("file")
    args = parser.parse_args(argv)

    try:
        if args.command == "export":
            settings = Settings.from_env()
            records = (
                export_sqlite(settings.sqlite_path)
                if settings.db_backend == "sqlite"
                else export_firestore(_db())
            )
            out = _open_out(args.file)
            try:
                n = write_ndjson(out, records)
            finally:
                if out is not sys.stdout:
                    out.close()
            print(f"dbexport: exported {n} documents", file=sys.stderr)
        elif args.command == "import":
            src = _open_in(args.file)
            try:
                n = import_records(_db(), read_ndjson(src), merge=args.merge)
            finally:
                if src is not sys.stdin:
                    src.close()
            print(f"dbexport: imported {n} documents", file=sys.stderr)
        else:
            out = _open_out(args.file)
            try:
                n = write_ndjson(out, convert_export(args.export_dir))
            finally:
                if out is not sys.stdout:
                    out.close()
            print(f"dbexport: converted {n} documents", file=sys.stderr)
    except sqlitestore.DatabaseInUse:
        print(
            "the relay is running and holds the database. From home/, run:\n"
            "  docker compose stop relay\n"
            f"  docker compose run --rm relay python -m app.dbexport import {args.file}\n"
            "  docker compose start relay",
            file=sys.stderr,
        )
        return 2
    except (NotEmpty, ConvertError, ValueError, FileNotFoundError) as exc:
        print(f"dbexport: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
