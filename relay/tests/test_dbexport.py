"""`python -m app.dbexport`: `gcloud firestore export` conversion, NDJSON and the export/import
round trip. `tests/fixtures/firestore_export/` is a real Firestore-emulator export."""

from __future__ import annotations

import io
import json
import os
import struct
from datetime import UTC, datetime
from pathlib import Path

import google_crc32c
import pytest

from app import dbexport
from app.config import Settings
from app.db.firestore import get_db

SQLITE = (os.environ.get("DB_BACKEND", "").strip().lower() or "firestore") == "sqlite"
needs_sqlite = pytest.mark.skipif(
    not SQLITE, reason="reads the SQLite file; there is none under Firestore"
)

FIXTURE = Path(__file__).parent / "fixtures" / "firestore_export"
OUTPUT0 = FIXTURE / "all_namespaces" / "all_kinds" / "output-0"

EXPECTED = [
    {"path": "devices/missing/locations/l1", "data": {"lat": 1.0}},
    {
        "path": "users/u1",
        "data": {
            "empty": [],
            "neg": -7,
            "ts": {"$ts": 1793448000123456},
            "i": 42,
            "b": True,
            "n": None,
            "s": "héllo",
            "emptymap": {},
            "f": 1.5,
            "arr": [1, "two", {"x": 3, "y": [1]}, None, {"$b64": "Ag=="}],
            "raw": {"$b64": "AAH/"},
            "big": 1152921504606846976,
            "m": {"a": {"b": [True, 2.0]}, "dotted.key": 1, "`q`": 2},
        },
    },
    {"path": "users/u1/pushSubscriptions/p_x", "data": {"endpoint": "https://e"}},
    {"path": "weird id/a b:c", "data": {"k": "v"}},
]


def _frame(rtype: int, payload: bytes) -> bytes:
    raw = google_crc32c.value(bytes([rtype]) + payload)
    masked = (((raw >> 15) | (raw << 17)) + 0xA282EAD8) & 0xFFFFFFFF
    return struct.pack("<IHB", masked, len(payload), rtype) + payload


def write_log(payloads: list[bytes]) -> bytes:
    """A LevelDB log file holding each payload as one record."""
    block = 32768
    out = bytearray()
    for payload in payloads:
        first = True
        rest = payload
        while True:
            left = block - len(out) % block
            if left < 7:
                out += b"\x00" * left
                continue
            chunk, rest = rest[: left - 7], rest[left - 7 :]
            if first and not rest:
                rtype = 1
            elif first:
                rtype = 2
            elif rest:
                rtype = 3
            else:
                rtype = 4
            out += _frame(rtype, chunk)
            first = False
            if not rest:
                break
    return bytes(out)


def varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def ld(field: int, data: bytes) -> bytes:
    return varint(field << 3 | 2) + varint(len(data)) + data


def vi(field: int, n: int) -> bytes:
    return varint(field << 3) + varint(n)


def entity(prop: bytes) -> bytes:
    element = varint(1 << 3 | 3) + ld(2, b"things") + ld(4, b"t1") + varint(1 << 3 | 4)
    return ld(13, ld(14, element)) + ld(14, prop)


def prop(name: str, value: bytes, meaning: int | None = None) -> bytes:
    return (
        (vi(1, meaning) if meaning is not None else b"")
        + ld(3, name.encode())
        + ld(5, value)
    )


def fixture_payloads() -> list[bytes]:
    return list(dbexport._log_records(OUTPUT0.read_bytes()))


def test_convert_fixture_exact():
    got = list(dbexport.convert_export(FIXTURE))
    assert got == EXPECTED
    data = got[1]["data"]
    assert type(data["f"]) is float and type(data["m"]["a"]["b"][1]) is float
    assert type(data["big"]) is int and type(data["i"]) is int
    assert data["b"] is True


def test_convert_missing_files(tmp_path):
    with pytest.raises(dbexport.ConvertError):
        list(dbexport.convert_export(tmp_path))


def test_log_records_fixture_reframed_and_decoded():
    payloads = fixture_payloads()
    assert len(payloads) == 4
    assert list(dbexport._log_records(write_log(payloads))) == payloads
    entities = sorted(dbexport._entity(p) for p in payloads)
    assert [{"path": p, "data": d} for p, d in entities] == EXPECTED


def _split_payloads() -> list[bytes]:
    big = bytes(range(256)) * 274  # 70144 bytes: FIRST, MIDDLE..., LAST
    assert len(big) > 2 * 32768
    # leaves exactly 3 bytes at the end of block 0, which are padding
    filler = b"f" * (32768 - 7 - 3)
    # leaves exactly 7 bytes (a zero-length FIRST fragment follows)
    filler2 = b"g" * (32768 - 7 - 7)
    return [b"a", big, filler, b"after-pad", b"x" * 40000, filler2, b"tail" * 10, b"z"]


@pytest.mark.parametrize(
    ("payloads", "min_len", "padding"),
    [
        pytest.param(_split_payloads(), 3 * 32768, None, id="split_and_block_padding"),
        # 5 bytes left in block 0
        pytest.param([b"f" * (32768 - 7 - 5), b"next"], 0, (32768 - 5, 5), id="block_end_padding_layout"),
    ],
)
def test_log_records_round_trip_across_block_padding(
    payloads: list[bytes], min_len: int, padding: tuple[int, int] | None
):
    data = write_log(payloads)
    assert len(data) > min_len
    if padding is not None:
        start, n = padding
        assert data[start : start + n] == b"\x00" * n
    assert list(dbexport._log_records(data)) == payloads


def test_log_records_flipped_byte_is_convert_error():
    data = bytearray(write_log([b"hello world", b"second"]))
    data[10] ^= 0xFF
    with pytest.raises(dbexport.ConvertError):
        list(dbexport._log_records(bytes(data)))
    real = bytearray(OUTPUT0.read_bytes())
    real[len(real) // 2] ^= 0x01
    with pytest.raises(dbexport.ConvertError):
        list(dbexport._log_records(bytes(real)))


def test_crafted_entity_supported_value():
    path, data = dbexport._entity(entity(prop("n", vi(1, 5))))
    assert (path, data) == ("things/t1", {"n": 5})


@pytest.mark.parametrize(
    ("field", "value", "meaning"),
    [
        pytest.param(
            "where",
            varint(5 << 3 | 3)
            + varint(6 << 3 | 1)
            + struct.pack("<d", 1.5)
            + varint(7 << 3 | 1)
            + struct.pack("<d", 2.5)
            + varint(5 << 3 | 4),
            None,
            id="geo_point_group",
        ),
        pytest.param("spot", b"", 9, id="geo_meaning"),
        pytest.param("link", varint(12 << 3 | 3) + varint(12 << 3 | 4), None, id="reference_value"),
    ],
)
def test_unsupported_values_are_rejected_naming_the_field(field: str, value: bytes, meaning: int | None):
    with pytest.raises(dbexport.ConvertError, match=f"'{field}'"):
        dbexport._entity(entity(prop(field, value, meaning=meaning)))


def test_unsupported_value_fails_whole_convert(tmp_path):
    geo = varint(5 << 3 | 3) + varint(5 << 3 | 4)
    d = tmp_path / "all_namespaces" / "all_kinds"
    d.mkdir(parents=True)
    (d / "output-0").write_bytes(write_log([entity(prop("where", geo))]))
    with pytest.raises(dbexport.ConvertError, match="where"):
        list(dbexport.convert_export(tmp_path))


def test_read_ndjson_skips_blank_lines_and_roundtrips():
    buf = io.StringIO()
    assert dbexport.write_ndjson(buf, EXPECTED) == 4
    text = "\n" + buf.getvalue().replace("\n", "\n\n  \n")
    assert list(dbexport.read_ndjson(io.StringIO(text))) == EXPECTED


@pytest.mark.parametrize(
    "line",
    [
        '{"data": {}}',
        '{"path": "users/u1"}',
        '{"path": "users/u1", "data": []}',
        '{"path": "users", "data": {}}',
        '{"path": "users/u1/sub", "data": {}}',
        '{"path": "users//x", "data": {}}',
        "[1]",
    ],
)
def test_read_ndjson_rejects_bad_records(line):
    with pytest.raises(ValueError):
        list(dbexport.read_ndjson(io.StringIO(line + "\n")))


def _get(path: str) -> dict:
    parent, _, doc_id = path.rpartition("/")
    return get_db().collection(parent).document(doc_id).get().to_dict()


def _export_configured() -> list[dict]:
    if SQLITE:
        return list(dbexport.export_sqlite(Settings.from_env().sqlite_path))
    return list(dbexport.export_firestore(get_db()))


def test_import_then_read_back_typed_values_and_export_equal():
    records = list(dbexport.convert_export(FIXTURE))
    assert dbexport.import_records(get_db(), records) == 4

    u1 = _get("users/u1")
    assert u1["ts"].tzinfo is not None
    assert u1["ts"].utcoffset().total_seconds() == 0
    assert u1["ts"].microsecond == 123456
    assert u1["ts"] == datetime.fromtimestamp(1793448000.123456, UTC)
    assert u1["raw"] == b"\x00\x01\xff"
    assert u1["n"] is None
    assert u1["empty"] == [] and u1["emptymap"] == {}
    assert u1["m"] == {"a": {"b": [True, 2.0]}, "dotted.key": 1, "`q`": 2}
    assert u1["arr"] == [1, "two", {"x": 3, "y": [1]}, None, b"\x02"]
    assert u1["big"] == 1152921504606846976 and u1["neg"] == -7
    assert u1["b"] is True and u1["f"] == 1.5 and u1["s"] == "héllo"
    assert _get("weird id/a b:c") == {"k": "v"}
    assert _get("devices/missing/locations/l1") == {"lat": 1.0}

    assert _export_configured() == sorted(records, key=lambda r: r["path"])


def test_import_refuses_non_empty_store():
    db = get_db()
    db.collection("users").document("u0").set({"a": 1})
    with pytest.raises(dbexport.NotEmpty):
        dbexport.import_records(db, EXPECTED)
    assert db.collection("weird id").document("a b:c").get().exists is False


def test_import_merge_replaces_same_path_keeps_others():
    db = get_db()
    db.collection("users").document("u1").set({"old": 1, "keep": 2})
    db.collection("users").document("other").set({"z": 1})
    n = dbexport.import_records(db, EXPECTED, merge=True)
    assert n == 4
    u1 = _get("users/u1")
    assert "old" not in u1 and "keep" not in u1 and u1["i"] == 42
    assert _get("users/other") == {"z": 1}


def test_import_batches_past_batch_size():
    total = dbexport.BATCH * 2 + 100
    assert total == 900
    records = ({"path": f"bulk/d{i:04d}", "data": {"i": i}} for i in range(total))
    assert dbexport.import_records(get_db(), records) == total
    docs = list(get_db().collection("bulk").stream())
    assert len(docs) == total
    assert _get("bulk/d0399") == {"i": 399}
    assert _get("bulk/d0400") == {"i": 400}
    assert _get("bulk/d0899") == {"i": 899}


def test_cli_convert_import_export(tmp_path, capsys):
    out = tmp_path / "records.ndjson"
    assert dbexport.main(["convert", str(FIXTURE), str(out)]) == 0
    assert "converted 4 documents" in capsys.readouterr().err
    lines = out.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 4
    assert [json.loads(line) for line in lines] == EXPECTED

    assert dbexport.main(["import", str(out)]) == 0
    assert "imported 4 documents" in capsys.readouterr().err

    # now non-empty
    assert dbexport.main(["import", str(out)]) == 1
    assert "--merge" in capsys.readouterr().err
    assert dbexport.main(["import", "--merge", str(out)]) == 0
    capsys.readouterr()

    assert dbexport.main(["export", "-"]) == 0
    cap = capsys.readouterr()
    assert "dbexport: exported 4 documents" in cap.err
    assert [json.loads(line) for line in cap.out.splitlines()] == EXPECTED

    dump = tmp_path / "dump.ndjson"
    assert dbexport.main(["export", str(dump)]) == 0
    assert "exported 4 documents" in capsys.readouterr().err
    # key order inside a document is the engine's, not part of the contract
    assert [
        json.loads(x) for x in dump.read_text(encoding="utf-8").splitlines()
    ] == EXPECTED


def test_cli_bad_input_returns_1(tmp_path, capsys):
    bad = tmp_path / "bad.ndjson"
    bad.write_text('{"path": "users", "data": {}}\n', encoding="utf-8")
    assert dbexport.main(["import", str(bad)]) == 1
    assert dbexport.main(["import", str(tmp_path / "nope.ndjson")]) == 1
    assert dbexport.main(["convert", str(tmp_path), str(tmp_path / "o")]) == 1
    capsys.readouterr()


@needs_sqlite
def test_export_sqlite_while_client_holds_the_lock():
    db = get_db()
    db.collection("users").document("u1").set({"a": 1})
    path = Settings.from_env().sqlite_path
    # the process's own client is open and holds the database
    assert list(dbexport.export_sqlite(path)) == [
        {"path": "users/u1", "data": {"a": 1}}
    ]
    db.collection("users").document("u2").set({"a": 2})
    assert [r["path"] for r in dbexport.export_sqlite(path)] == ["users/u1", "users/u2"]


@needs_sqlite
def test_export_sqlite_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        list(dbexport.export_sqlite(str(tmp_path / "nope.sqlite")))
